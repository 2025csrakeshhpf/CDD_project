import asyncio
import json
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from main import DB_PATH, decode_token, get_current_user
from models import User
from sandbox_judge import execute_sandboxed_code

router = APIRouter(tags=["1v1 Peer Coding Duels"])

ALLOWED_DURATIONS = {15, 30, 45, 60}
ALLOWED_LANGUAGES = {"python", "c", "cpp", "java", "javascript"}


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_duel_tables():
    conn = _conn()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS duel_challenges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            challenge_uid TEXT UNIQUE NOT NULL,
            challenger_email TEXT NOT NULL,
            challenger_name TEXT NOT NULL,
            opponent_email TEXT NOT NULL,
            opponent_name TEXT NOT NULL,
            problem_id INTEGER NOT NULL,
            difficulty_pref TEXT DEFAULT 'Easy',
            duration_mins INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'Pending', -- Pending | Accepted | Rejected | Cancelled | Active | Completed
            challenger_ready INTEGER DEFAULT 0,
            opponent_ready INTEGER DEFAULT 0,
            started_at TEXT DEFAULT '',
            ends_at TEXT DEFAULT '',
            winner_email TEXT DEFAULT '',
            is_draw INTEGER DEFAULT 0,
            rating_delta INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS duel_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            challenge_id INTEGER NOT NULL,
            user_email TEXT NOT NULL,
            language TEXT NOT NULL,
            source_code TEXT NOT NULL,
            verdict TEXT NOT NULL,
            passed_cases INTEGER DEFAULT 0,
            total_cases INTEGER DEFAULT 0,
            exec_time_ms REAL DEFAULT 0.0,
            memory_kb INTEGER DEFAULT 14500,
            base_points INTEGER DEFAULT 0,
            bonus_points INTEGER DEFAULT 0,
            speed_bonus INTEGER DEFAULT 0,
            total_score INTEGER DEFAULT 0,
            elapsed_sec INTEGER DEFAULT 0,
            submitted_at TEXT NOT NULL,
            UNIQUE(challenge_id, user_email)
        );

        CREATE TABLE IF NOT EXISTS duel_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            challenge_uid TEXT NOT NULL,
            message TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


init_duel_tables()


# -------------------- REAL-TIME WEBSOCKET ROOM MANAGER --------------------
class DuelRoomManager:
    def __init__(self):
        self.rooms: Dict[str, Dict[str, WebSocket]] = {}

    async def connect(self, challenge_uid: str, user_email: str, ws: WebSocket):
        await ws.accept()
        self.rooms.setdefault(challenge_uid, {})[user_email] = ws

    def disconnect(self, challenge_uid: str, user_email: str):
        if challenge_uid in self.rooms and user_email in self.rooms[challenge_uid]:
            del self.rooms[challenge_uid][user_email]

    async def broadcast(self, challenge_uid: str, payload: Dict, exclude_email: Optional[str] = None):
        room = self.rooms.get(challenge_uid, {})
        dead = []
        for email, ws in room.items():
            if exclude_email and email == exclude_email:
                continue
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(email)
        for email in dead:
            self.disconnect(challenge_uid, email)


room_manager = DuelRoomManager()


# -------------------- SCHEMAS --------------------
class DuelCreateReq(BaseModel):
    opponent_email: str
    problem_id: Optional[int] = None
    difficulty: str = "Easy"
    duration_mins: int = 30


class DuelCodeReq(BaseModel):
    language: str
    code: str
    custom_input: str = ""


# -------------------- SCORING & WINNER RESOLUTION --------------------
def _compute_duel_score(passed_cases: int, total_cases: int, all_passed: bool, elapsed_sec: int, duration_mins: int) -> Dict[str, int]:
    base_points = passed_cases * 10
    bonus_points = 20 if all_passed else 0
    speed_bonus = 0
    if all_passed:
        total_sec = max(60, duration_mins * 60)
        remaining_ratio = max(0.0, min(1.0, (total_sec - elapsed_sec) / total_sec))
        speed_bonus = int(round(15.0 * remaining_ratio))
    return {
        "base_points": base_points,
        "bonus_points": bonus_points,
        "speed_bonus": speed_bonus,
        "total_score": base_points + bonus_points + speed_bonus,
    }


def _finalize_duel_if_ready(conn: sqlite3.Connection, challenge: sqlite3.Row) -> Optional[Dict]:
    cur = conn.cursor()
    cid = challenge["id"]
    if challenge["status"] == "Completed":
        cur.execute("SELECT * FROM duel_submissions WHERE challenge_id = ?", (cid,))
        subs = [dict(r) for r in cur.fetchall()]
        for s in subs:
            s.pop("source_code", None)
        return {"status": "Completed", "winner_email": challenge["winner_email"], "is_draw": bool(challenge["is_draw"]), "submissions": subs}

    cur.execute("SELECT * FROM duel_submissions WHERE challenge_id = ?", (cid,))
    subs = [dict(r) for r in cur.fetchall()]

    expired = False
    if challenge["ends_at"]:
        ends_dt = datetime.fromisoformat(challenge["ends_at"])
        if datetime.utcnow() >= ends_dt:
            expired = True

    if len(subs) < 2 and not expired:
        return None

    # Ensure non-submitting participant gets 0 score on timer expiration
    submitted_emails = {s["user_email"] for s in subs}
    for email in (challenge["challenger_email"], challenge["opponent_email"]):
        if email not in submitted_emails:
            subs.append({
                "challenge_id": cid,
                "user_email": email,
                "language": "-",
                "verdict": "NOT SUBMITTED",
                "passed_cases": 0,
                "total_cases": 0,
                "exec_time_ms": 999999.0,
                "memory_kb": 0,
                "base_points": 0,
                "bonus_points": 0,
                "speed_bonus": 0,
                "total_score": 0,
                "elapsed_sec": challenge["duration_mins"] * 60,
                "submitted_at": "9999-12-31 23:59:59",
            })

    s1, s2 = subs[0], subs[1]

    # Tie-breaker tuple: (1) Highest score, (2) Highest passed cases, (3) Lowest exec_time_ms, (4) Earliest accepted timestamp
    def rank_key(s: Dict):
        early_ts = s["submitted_at"] if s["verdict"] == "ACCEPTED" else "9999-12-31 23:59:59"
        return (
            s["total_score"],
            s["passed_cases"],
            -float(s["exec_time_ms"]),
            early_ts,
        )

    k1 = (s1["total_score"], s1["passed_cases"], -float(s1["exec_time_ms"]))
    k2 = (s2["total_score"], s2["passed_cases"], -float(s2["exec_time_ms"]))
    t1 = s1["submitted_at"] if s1["verdict"] == "ACCEPTED" else "9999-12-31 23:59:59"
    t2 = s2["submitted_at"] if s2["verdict"] == "ACCEPTED" else "9999-12-31 23:59:59"

    is_draw = 0
    winner_email = ""
    rating_delta = 25

    if k1 == k2 and t1 == t2:
        is_draw = 1
        rating_delta = 5
    else:
        s1_wins = (k1 > k2) or (k1 == k2 and t1 < t2)
        winner_email = s1["user_email"] if s1_wins else s2["user_email"]
        loser_email = s2["user_email"] if s1_wins else s1["user_email"]

        # Update ratings in user_ratings table if present
        cur.execute("CREATE TABLE IF NOT EXISTS user_ratings (user_email TEXT PRIMARY KEY, rating INTEGER DEFAULT 1200, contests_played INTEGER DEFAULT 0, best_rank INTEGER DEFAULT 0)")
        cur.execute("INSERT INTO user_ratings (user_email, rating, contests_played, best_rank) VALUES (?, 1225, 1, 1) ON CONFLICT(user_email) DO UPDATE SET rating = user_ratings.rating + 25, contests_played = user_ratings.contests_played + 1", (winner_email,))
        cur.execute("INSERT INTO user_ratings (user_email, rating, contests_played, best_rank) VALUES (?, 1185, 1, 1) ON CONFLICT(user_email) DO UPDATE SET rating = MAX(800, user_ratings.rating - 15), contests_played = user_ratings.contests_played + 1", (loser_email,))

    if is_draw:
        cur.execute("CREATE TABLE IF NOT EXISTS user_ratings (user_email TEXT PRIMARY KEY, rating INTEGER DEFAULT 1200, contests_played INTEGER DEFAULT 0, best_rank INTEGER DEFAULT 0)")
        for em in (s1["user_email"], s2["user_email"]):
            cur.execute("INSERT INTO user_ratings (user_email, rating, contests_played, best_rank) VALUES (?, 1205, 1, 1) ON CONFLICT(user_email) DO UPDATE SET rating = user_ratings.rating + 5, contests_played = user_ratings.contests_played + 1", (em,))

    cur.execute(
        "UPDATE duel_challenges SET status = 'Completed', winner_email = ?, is_draw = ?, rating_delta = ? WHERE id = ?",
        (winner_email, is_draw, rating_delta, cid),
    )
    conn.commit()

    for s in subs:
        s.pop("source_code", None)
    return {"status": "Completed", "winner_email": winner_email, "is_draw": bool(is_draw), "rating_delta": rating_delta, "submissions": subs}


# -------------------- API ENDPOINTS --------------------
@router.post("/api/duels/create")
def create_duel_challenge(req: DuelCreateReq, current_user: User = Depends(get_current_user)):
    opp_email = req.opponent_email.strip().lower()
    if opp_email == current_user.email.lower():
        raise HTTPException(status_code=400, detail="You cannot challenge yourself.")
    if req.duration_mins not in ALLOWED_DURATIONS:
        raise HTTPException(status_code=400, detail="Duration must be 15, 30, 45, or 60 minutes.")

    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT name, email FROM users WHERE email = ?", (opp_email,))
    opp = cur.fetchone()
    if not opp:
        conn.close()
        raise HTTPException(status_code=404, detail="Opponent email is not a registered student.")

    # Resolve problem either by explicit problem_id or random problem matching difficulty
    if req.problem_id:
        cur.execute("SELECT id, title, difficulty FROM coding_problems WHERE id = ?", (req.problem_id,))
    else:
        cur.execute("SELECT id, title, difficulty FROM coding_problems WHERE difficulty = ? ORDER BY RANDOM() LIMIT 1", (req.difficulty,))
    prob = cur.fetchone()
    if not prob:
        cur.execute("SELECT id, title, difficulty FROM coding_problems LIMIT 1")
        prob = cur.fetchone()

    uid = secrets.token_hex(4).upper()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        INSERT INTO duel_challenges (
            challenge_uid, challenger_email, challenger_name, opponent_email, opponent_name,
            problem_id, difficulty_pref, duration_mins, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?)
        """,
        (uid, current_user.email, current_user.name, opp["email"], opp["name"], prob["id"], prob["difficulty"], req.duration_mins, now),
    )
    cur.execute(
        "INSERT INTO duel_notifications (user_email, challenge_uid, message, is_read, created_at) VALUES (?, ?, ?, 0, ?)",
        (opp["email"], uid, f"⚔️ {current_user.name} challenged you to a {req.duration_mins}-min 1v1 Duel on '{prob['title']}' ({prob['difficulty']})!", now),
    )
    conn.commit()
    conn.close()
    return {"challenge_uid": uid, "status": "Pending", "problem_title": prob["title"], "duration_mins": req.duration_mins}


@router.get("/api/duels/list")
def list_user_duels(current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT d.*, p.title as problem_title, p.difficulty as problem_difficulty, p.topic as problem_topic
        FROM duel_challenges d
        JOIN coding_problems p ON p.id = d.problem_id
        WHERE d.challenger_email = ? OR d.opponent_email = ?
        ORDER BY d.id DESC
        """,
        (current_user.email, current_user.email),
    )
    duels = []
    for row in cur.fetchall():
        d = dict(row)
        if d["status"] == "Active" and d["ends_at"] and datetime.utcnow() >= datetime.fromisoformat(d["ends_at"]):
            _finalize_duel_if_ready(conn, row)
            cur.execute("SELECT * FROM duel_challenges WHERE id = ?", (d["id"],))
            updated = dict(cur.fetchone())
            d.update(updated)
        duels.append(d)

    cur.execute("SELECT * FROM duel_notifications WHERE user_email = ? AND is_read = 0 ORDER BY id DESC", (current_user.email,))
    notifs = [dict(n) for n in cur.fetchall()]
    conn.close()
    return {"duels": duels, "notifications": notifs}


@router.post("/api/duels/{challenge_uid}/respond")
async def respond_to_duel(challenge_uid: str, action: str, current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM duel_challenges WHERE challenge_uid = ?", (challenge_uid,))
    d = cur.fetchone()
    if not d:
        conn.close()
        raise HTTPException(status_code=404, detail="Challenge not found.")

    act = action.strip().lower()
    if act == "cancel":
        if d["challenger_email"] != current_user.email or d["status"] != "Pending":
            conn.close()
            raise HTTPException(status_code=403, detail="Only challenger can cancel a pending duel.")
        cur.execute("UPDATE duel_challenges SET status = 'Cancelled' WHERE id = ?", (d["id"],))
    elif act in ("accept", "reject"):
        if d["opponent_email"] != current_user.email or d["status"] != "Pending":
            conn.close()
            raise HTTPException(status_code=403, detail="Only invited opponent can accept or reject.")
        new_status = "Accepted" if act == "accept" else "Rejected"
        cur.execute("UPDATE duel_challenges SET status = ? WHERE id = ?", (new_status, d["id"]))
        cur.execute("UPDATE duel_notifications SET is_read = 1 WHERE challenge_uid = ? AND user_email = ?", (challenge_uid, current_user.email))
    else:
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid action.")

    conn.commit()
    conn.close()
    await room_manager.broadcast(challenge_uid, {"type": "STATUS_UPDATE", "action": act})
    return {"challenge_uid": challenge_uid, "action": act}


@router.post("/api/duels/{challenge_uid}/ready")
async def mark_participant_ready(challenge_uid: str, current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM duel_challenges WHERE challenge_uid = ?", (challenge_uid,))
    d = cur.fetchone()
    if not d:
        conn.close()
        raise HTTPException(status_code=404, detail="Challenge not found.")
    if d["status"] not in ("Accepted", "Active"):
        conn.close()
        raise HTTPException(status_code=400, detail="Challenge must be Accepted before readying up.")

    is_challenger = current_user.email == d["challenger_email"]
    is_opponent = current_user.email == d["opponent_email"]
    if not (is_challenger or is_opponent):
        conn.close()
        raise HTTPException(status_code=403, detail="Not a participant in this duel.")

    c_ready = 1 if is_challenger else d["challenger_ready"]
    o_ready = 1 if is_opponent else d["opponent_ready"]

    started_at = d["started_at"]
    ends_at = d["ends_at"]
    status = d["status"]

    # Start server-side countdown ONLY when BOTH participants are ready
    if c_ready == 1 and o_ready == 1 and status == "Accepted":
        now_utc = datetime.utcnow()
        started_at = now_utc.isoformat()
        ends_at = (now_utc + timedelta(minutes=d["duration_mins"])).isoformat()
        status = "Active"

    cur.execute(
        "UPDATE duel_challenges SET challenger_ready = ?, opponent_ready = ?, status = ?, started_at = ?, ends_at = ? WHERE id = ?",
        (c_ready, o_ready, status, started_at, ends_at, d["id"]),
    )
    conn.commit()
    conn.close()

    await room_manager.broadcast(
        challenge_uid,
        {"type": "READY_STATE", "challenger_ready": bool(c_ready), "opponent_ready": bool(o_ready), "status": status, "ends_at": ends_at},
    )
    return {"status": status, "challenger_ready": bool(c_ready), "opponent_ready": bool(o_ready), "ends_at": ends_at}


@router.get("/api/duels/{challenge_uid}")
def get_duel_state(challenge_uid: str, current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM duel_challenges WHERE challenge_uid = ?", (challenge_uid,))
    d = cur.fetchone()
    if not d:
        conn.close()
        raise HTTPException(status_code=404, detail="Duel not found.")
    if current_user.email not in (d["challenger_email"], d["opponent_email"]):
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied.")

    # Auto-finalize if timer expired
    if d["status"] == "Active" and d["ends_at"] and datetime.utcnow() >= datetime.fromisoformat(d["ends_at"]):
        _finalize_duel_if_ready(conn, d)
        cur.execute("SELECT * FROM duel_challenges WHERE id = ?", (d["id"],))
        d = cur.fetchone()

    cur.execute("SELECT id, problem_number, title, topic, difficulty, description, input_format, output_format, constraints, starters_json FROM coding_problems WHERE id = ?", (d["problem_id"],))
    prob = dict(cur.fetchone())
    prob["starters"] = json.loads(prob.pop("starters_json") or "{}")

    # Return ONLY visible sample test cases (hidden cases stay strictly on backend)
    cur.execute("SELECT input_data, expected_output FROM test_cases WHERE problem_id = ? AND is_sample = 1", (d["problem_id"],))
    prob["samples"] = [dict(s) for s in cur.fetchall()]

    # Load submissions WITHOUT exposing opponent's source_code
    cur.execute("SELECT * FROM duel_submissions WHERE challenge_id = ?", (d["id"],))
    subs = []
    for s in cur.fetchall():
        sd = dict(s)
        if sd["user_email"] != current_user.email:
            sd.pop("source_code", None)
        subs.append(sd)

    remaining_sec = 0
    if d["status"] == "Active" and d["ends_at"]:
        remaining_sec = max(0, int((datetime.fromisoformat(d["ends_at"]) - datetime.utcnow()).total_seconds()))

    conn.close()
    return {
        "duel": dict(d),
        "problem": prob,
        "submissions": subs,
        "remaining_sec": remaining_sec,
    }


@router.post("/api/duels/{challenge_uid}/run")
async def run_duel_code(challenge_uid: str, req: DuelCodeReq, current_user: User = Depends(get_current_user)):
    lang = req.language.strip().lower()
    if lang not in ALLOWED_LANGUAGES:
        raise HTTPException(status_code=400, detail="Supported duel languages: C, C++, Java, Python, JavaScript.")

    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM duel_challenges WHERE challenge_uid = ?", (challenge_uid,))
    d = cur.fetchone()
    if not d or d["status"] != "Active":
        conn.close()
        raise HTTPException(status_code=400, detail="Duel is not currently active.")

    if req.custom_input.strip():
        cases = [{"input": req.custom_input, "expected": "", "is_sample": True}]
    else:
        cur.execute("SELECT input_data as input, expected_output as expected, is_sample FROM test_cases WHERE problem_id = ? AND is_sample = 1", (d["problem_id"],))
        cases = [dict(r) for r in cur.fetchall()]
    conn.close()

    res = execute_sandboxed_code(lang, req.code, cases)
    await room_manager.broadcast(
        challenge_uid,
        {"type": "OPPONENT_RAN_CODE", "user_email": current_user.email, "passed_cases": res["passed_cases"], "total_cases": res["total_cases"]},
        exclude_email=current_user.email,
    )
    return res


@router.post("/api/duels/{challenge_uid}/submit")
async def submit_duel_code(challenge_uid: str, req: DuelCodeReq, current_user: User = Depends(get_current_user)):
    lang = req.language.strip().lower()
    if lang not in ALLOWED_LANGUAGES:
        raise HTTPException(status_code=400, detail="Supported duel languages: C, C++, Java, Python, JavaScript.")

    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM duel_challenges WHERE challenge_uid = ?", (challenge_uid,))
    d = cur.fetchone()
    if not d:
        conn.close()
        raise HTTPException(status_code=404, detail="Duel not found.")
    if d["status"] != "Active":
        conn.close()
        raise HTTPException(status_code=400, detail="Duel is no longer active.")

    # Check timer expiration
    now_utc = datetime.utcnow()
    if d["ends_at"] and now_utc >= datetime.fromisoformat(d["ends_at"]):
        final = _finalize_duel_if_ready(conn, d)
        conn.close()
        raise HTTPException(status_code=400, detail="Time has expired for this duel.")

    # Prevent duplicate submissions from same participant
    cur.execute("SELECT id FROM duel_submissions WHERE challenge_id = ? AND user_email = ?", (d["id"], current_user.email))
    if cur.fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="You have already submitted your final solution for this duel.")

    # Evaluate against ALL visible + hidden test cases
    cur.execute("SELECT input_data as input, expected_output as expected, is_sample FROM test_cases WHERE problem_id = ?", (d["problem_id"],))
    all_cases = [dict(r) for r in cur.fetchall()]
    judge = execute_sandboxed_code(lang, req.code, all_cases)

    all_passed = (judge["verdict"] == "ACCEPTED") and (judge["passed_cases"] == judge["total_cases"]) and (judge["total_cases"] > 0)
    started_dt = datetime.fromisoformat(d["started_at"]) if d["started_at"] else now_utc
    elapsed_sec = max(1, int((now_utc - started_dt).total_seconds()))

    scores = _compute_duel_score(judge["passed_cases"], judge["total_cases"], all_passed, elapsed_sec, d["duration_mins"])
    submitted_ts = now_utc.strftime("%Y-%m-%d %H:%M:%S.%f")

    try:
        cur.execute(
            """
            INSERT INTO duel_submissions (
                challenge_id, user_email, language, source_code, verdict, passed_cases, total_cases,
                exec_time_ms, memory_kb, base_points, bonus_points, speed_bonus, total_score, elapsed_sec, submitted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                d["id"], current_user.email, lang, req.code, judge["verdict"],
                judge["passed_cases"], judge["total_cases"], judge["execution_time_ms"], 14500,
                scores["base_points"], scores["bonus_points"], scores["speed_bonus"], scores["total_score"],
                elapsed_sec, submitted_ts,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=409, detail="Duplicate submission blocked.")

    final_result = _finalize_duel_if_ready(conn, d)
    conn.close()

    await room_manager.broadcast(
        challenge_uid,
        {
            "type": "OPPONENT_SUBMITTED",
            "user_email": current_user.email,
            "verdict": judge["verdict"],
            "passed_cases": judge["passed_cases"],
            "total_cases": judge["total_cases"],
            "completed": bool(final_result),
            "final_result": final_result,
        },
    )

    return {
        "verdict": judge["verdict"],
        "passed_cases": judge["passed_cases"],
        "total_cases": judge["total_cases"],
        "execution_time_ms": judge["execution_time_ms"],
        "memory_kb": 14500,
        "scores": scores,
        "case_results": judge["case_results"],
        "final_result": final_result,
    }


@router.websocket("/ws/duel/{challenge_uid}")
async def duel_websocket_endpoint(ws: WebSocket, challenge_uid: str, token: str = ""):
    try:
        claims = decode_token(token)
        user_email = claims["email"]
    except Exception:
        await ws.close(code=1008)
        return

    await room_manager.connect(challenge_uid, user_email, ws)
    await room_manager.broadcast(challenge_uid, {"type": "OPPONENT_CONNECTED", "user_email": user_email}, exclude_email=user_email)
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            if msg.get("type") == "TYPING":
                await room_manager.broadcast(
                    challenge_uid,
                    {"type": "OPPONENT_CODING", "user_email": user_email, "language": msg.get("language", "python")},
                    exclude_email=user_email,
                )
    except WebSocketDisconnect:
        room_manager.disconnect(challenge_uid, user_email)
        await room_manager.broadcast(challenge_uid, {"type": "OPPONENT_DISCONNECTED", "user_email": user_email}, exclude_email=user_email)
