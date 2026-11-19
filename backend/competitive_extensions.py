import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from main import DB_PATH, get_current_user
from models import User
from sandbox_judge import execute_sandboxed_code

router = APIRouter(prefix="/api/competitive", tags=["Competitive & Community"])


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_competitive_tables():
    conn = _conn()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS user_ratings (
            user_email TEXT PRIMARY KEY,
            rating INTEGER DEFAULT 1200,
            contests_played INTEGER DEFAULT 0,
            best_rank INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS weekly_contests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            duration_mins INTEGER DEFAULT 60,
            problem_ids_json TEXT NOT NULL,
            status TEXT DEFAULT 'LIVE',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS contest_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id INTEGER NOT NULL,
            user_email TEXT NOT NULL,
            user_name TEXT NOT NULL,
            solved_count INTEGER DEFAULT 0,
            score INTEGER DEFAULT 0,
            time_taken_sec INTEGER DEFAULT 0,
            rating_delta INTEGER DEFAULT 0,
            submitted_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS problem_discussions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            problem_id INTEGER NOT NULL,
            user_email TEXT NOT NULL,
            user_name TEXT NOT NULL,
            approach_tag TEXT DEFAULT 'Optimal Approach',
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS forum_threads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            user_name TEXT NOT NULL,
            category TEXT DEFAULT 'DSA & Contests',
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS peer_challenges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            challenger_email TEXT NOT NULL,
            challenger_name TEXT NOT NULL,
            opponent_email TEXT NOT NULL,
            problem_id INTEGER NOT NULL,
            problem_title TEXT NOT NULL,
            status TEXT DEFAULT 'OPEN',
            winner_email TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS prep_assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            track TEXT NOT NULL,
            score REAL NOT NULL,
            time_taken_sec INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )

    cur.execute("SELECT COUNT(*) FROM weekly_contests")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO weekly_contests (title, description, duration_mins, problem_ids_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("VTU Weekly Rated Contest #14", "3 Algorithmic Problems (Easy, Medium, Hard) • Rated ELO Scoring", 60, json.dumps([1, 2, 3]), "LIVE", datetime.now().strftime("%Y-%m-%d")),
                ("Campus Placement Sprint #9", "Arrays, Sliding Window & Dynamic Programming Speed Round", 45, json.dumps([4, 5, 6]), "LIVE", datetime.now().strftime("%Y-%m-%d")),
            ],
        )

    cur.execute("SELECT COUNT(*) FROM forum_threads")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO forum_threads (user_email, user_name, category, title, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("rakesh@vtu.ac.in", "Rakesh HP", "Placement Prep", "Best order to master all 23 DSA topics before campus drives?", "Start with Arrays, Strings, Hash Tables, and Two Pointers before advancing to Dynamic Programming and Graphs.", datetime.now().strftime("%Y-%m-%d %H:%M")),
        )

    conn.commit()
    conn.close()


init_competitive_tables()


# -------------------- SCHEMAS --------------------
class CustomRunReq(BaseModel):
    language: str
    code: str
    custom_input: str = ""
    expected_output: Optional[str] = None


class DiscussionCreateReq(BaseModel):
    approach_tag: str = "Optimal Approach"
    content: str


class ContestSubmitReq(BaseModel):
    problem_id: int
    language: str
    code: str
    time_taken_sec: int = 120


class ForumPostReq(BaseModel):
    category: str = "DSA & Contests"
    title: str
    body: str


class PeerChallengeReq(BaseModel):
    opponent_email: str
    problem_id: int


class PrepSubmitReq(BaseModel):
    track: str
    score: float
    time_taken_sec: int = 60


# -------------------- ENDPOINTS --------------------
@router.get("/daily-challenge")
def get_daily_challenge(current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM coding_problems")
    total = cur.fetchone()[0] or 1
    day_seed = int(datetime.now().strftime("%Y%m%d"))
    target_num = (day_seed % min(total, 500)) + 1
    cur.execute("SELECT id, problem_number, title, topic, difficulty, company FROM coding_problems WHERE problem_number = ?", (target_num,))
    row = cur.fetchone()
    if not row:
        cur.execute("SELECT id, problem_number, title, topic, difficulty, company FROM coding_problems LIMIT 1")
        row = cur.fetchone()
    conn.close()
    return {"date": datetime.now().strftime("%Y-%m-%d"), "problem": dict(row)}


@router.post("/custom-run")
def run_custom_input(req: CustomRunReq, current_user: User = Depends(get_current_user)):
    cases = [{"input": req.custom_input, "expected": req.expected_output if req.expected_output is not None else "", "is_sample": True}]
    res = execute_sandboxed_code(req.language, req.code, cases)
    actual = res["case_results"][0]["actual"] if res["case_results"] else ""
    status = res["verdict"] if res["verdict"] in ("COMPILATION ERROR", "RUNTIME ERROR", "TIME LIMIT EXCEEDED") else "EXECUTED"
    return {
        "status": status,
        "output": actual,
        "error_message": res["error_message"],
        "execution_time_ms": res["execution_time_ms"],
    }


@router.get("/submissions/{problem_id}")
def get_problem_submission_history(problem_id: int, current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, language, verdict, passed, passed_cases, total_cases, execution_time_ms, time_taken_sec, submitted_at
        FROM student_submissions
        WHERE user_email = ? AND problem_id = ? AND mode = 'submit'
        ORDER BY id DESC LIMIT 20
        """,
        (current_user.email, problem_id),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


@router.get("/discussions/{problem_id}")
def list_problem_discussions(problem_id: int, current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM problem_discussions WHERE problem_id = ? ORDER BY id DESC", (problem_id,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


@router.post("/discussions/{problem_id}")
def post_problem_discussion(problem_id: int, req: DiscussionCreateReq, current_user: User = Depends(get_current_user)):
    if len(req.content.strip()) < 3:
        raise HTTPException(status_code=400, detail="Discussion message cannot be empty.")
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO problem_discussions (problem_id, user_email, user_name, approach_tag, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (problem_id, current_user.email, current_user.name, req.approach_tag, req.content.strip(), datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    conn.commit()
    conn.close()
    return {"status": "posted"}


@router.get("/contests")
def list_contests(current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM weekly_contests ORDER BY id ASC")
    contests = []
    for c in cur.fetchall():
        pids = json.loads(c["problem_ids_json"])
        cur.execute(f"SELECT id, problem_number, title, topic, difficulty FROM coding_problems WHERE id IN ({','.join('?'*len(pids))})", pids)
        probs = [dict(p) for p in cur.fetchall()]
        contests.append({
            "id": c["id"],
            "title": c["title"],
            "description": c["description"],
            "duration_mins": c["duration_mins"],
            "status": c["status"],
            "problems": probs,
        })

    cur.execute("SELECT * FROM user_ratings WHERE user_email = ?", (current_user.email,))
    r_row = cur.fetchone()
    rating_info = dict(r_row) if r_row else {"user_email": current_user.email, "rating": 1200, "contests_played": 0, "best_rank": 1}

    cur.execute("SELECT * FROM contest_entries WHERE user_email = ? ORDER BY id DESC LIMIT 10", (current_user.email,))
    history = [dict(h) for h in cur.fetchall()]
    conn.close()
    return {"contests": contests, "user_rating": rating_info, "history": history}


@router.post("/contests/{contest_id}/submit")
def submit_contest_solution(contest_id: int, req: ContestSubmitReq, current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT input_data, expected_output, is_sample FROM test_cases WHERE problem_id = ?", (req.problem_id,))
    cases = [{"input": r["input_data"], "expected": r["expected_output"], "is_sample": bool(r["is_sample"])} for r in cur.fetchall()]
    if not cases:
        conn.close()
        raise HTTPException(status_code=404, detail="Contest problem not found.")

    judge = execute_sandboxed_code(req.language, req.code, cases)
    accepted = judge["verdict"] == "ACCEPTED" and judge["passed_cases"] == judge["total_cases"]
    pts = 100 if accepted else int((judge["passed_cases"] / max(1, judge["total_cases"])) * 40)
    delta = 25 if accepted else -5

    cur.execute(
        "INSERT INTO contest_entries (contest_id, user_email, user_name, solved_count, score, time_taken_sec, rating_delta, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (contest_id, current_user.email, current_user.name, 1 if accepted else 0, pts, req.time_taken_sec, delta, datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    cur.execute(
        """
        INSERT INTO user_ratings (user_email, rating, contests_played, best_rank)
        VALUES (?, ?, 1, 1)
        ON CONFLICT(user_email) DO UPDATE SET
            rating = MAX(800, user_ratings.rating + ?),
            contests_played = user_ratings.contests_played + 1
        """,
        (current_user.email, 1200 + delta, delta),
    )
    conn.commit()
    conn.close()
    return {"verdict": judge["verdict"], "accepted": accepted, "points_earned": pts, "rating_delta": delta, "passed_cases": judge["passed_cases"], "total_cases": judge["total_cases"]}


@router.get("/leaderboard")
def get_leaderboards(college: Optional[str] = "All", current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()

    cur.execute("SELECT id, name, college, email FROM users WHERE role = 'student' ORDER BY id ASC")
    all_students = cur.fetchall()

    # Check optional tables safely
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r["name"] for r in cur.fetchall()}

    eligible_rows = []
    seen_user_ids = set()

    for u in all_students:
        uid = u["id"]
        if uid in seen_user_ids:
            continue
        seen_user_ids.add(uid)

        email_norm = (u["email"] or "").strip().lower()
        if college and college != "All" and college.lower() not in (u["college"] or "").lower():
            continue

        # Aggregate unique accepted problems from practice + completed duels
        solved_pids = set()
        if "student_submissions" in tables:
            cur.execute(
                "SELECT DISTINCT problem_id FROM student_submissions WHERE LOWER(TRIM(user_email)) = ? AND passed = 1",
                (email_norm,),
            )
            solved_pids.update(r["problem_id"] for r in cur.fetchall())

        if "duel_submissions" in tables:
            cur.execute(
                "SELECT DISTINCT problem_id FROM duel_submissions ds JOIN duel_challenges dc ON dc.id = ds.challenge_id WHERE LOWER(TRIM(ds.user_email)) = ? AND ds.verdict = 'ACCEPTED'",
                (email_norm,),
            )
            solved_pids.update(r["problem_id"] for r in cur.fetchall())

        # Count genuine completed contests / duels
        completed_contests = 0
        if "contest_entries" in tables:
            cur.execute("SELECT COUNT(*) FROM contest_entries WHERE LOWER(TRIM(user_email)) = ?", (email_norm,))
            completed_contests += cur.fetchone()[0] or 0

        if "duel_challenges" in tables:
            cur.execute(
                "SELECT COUNT(*) FROM duel_challenges WHERE status = 'Completed' AND (LOWER(TRIM(challenger_email)) = ? OR LOWER(TRIM(opponent_email)) = ?)",
                (email_norm, email_norm),
            )
            completed_contests += cur.fetchone()[0] or 0

        solved_count = len(solved_pids)

        # Eligibility rule: must have >= 1 accepted submission OR >= 1 completed contest
        if solved_count == 0 and completed_contests == 0:
            continue

        # Genuine rating calculation from actual contest rating + solved problem performance
        base_rating = 1200
        if "user_ratings" in tables:
            cur.execute("SELECT rating FROM user_ratings WHERE LOWER(TRIM(user_email)) = ?", (email_norm,))
            r_row = cur.fetchone()
            if r_row and r_row["rating"]:
                base_rating = int(r_row["rating"])

        computed_rating = base_rating + (solved_count * 15)
        eligible_rows.append({
            "user_id": uid,
            "name": u["name"],
            "college": u["college"],
            "email": u["email"],
            "solved_count": solved_count,
            "contests_played": completed_contests,
            "rating": computed_rating,
        })

    eligible_rows.sort(key=lambda x: (-x["rating"], -x["solved_count"], x["user_id"]))
    for idx, item in enumerate(eligible_rows, start=1):
        item["rank"] = idx

    conn.close()
    return eligible_rows[:50]


@router.get("/forum")
def list_forum_threads(current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM forum_threads ORDER BY id DESC LIMIT 30")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


@router.post("/forum")
def create_forum_thread(req: ForumPostReq, current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO forum_threads (user_email, user_name, category, title, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (current_user.email, current_user.name, req.category, req.title.strip(), req.body.strip(), datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    conn.commit()
    conn.close()
    return {"status": "created"}


@router.get("/challenges")
def list_peer_challenges(current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM peer_challenges WHERE challenger_email = ? OR opponent_email = ? ORDER BY id DESC",
        (current_user.email, current_user.email),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


@router.post("/challenges")
def create_peer_challenge(req: PeerChallengeReq, current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT title FROM coding_problems WHERE id = ?", (req.problem_id,))
    p = cur.fetchone()
    if not p:
        conn.close()
        raise HTTPException(status_code=404, detail="Problem not found.")
    cur.execute(
        "INSERT INTO peer_challenges (challenger_email, challenger_name, opponent_email, problem_id, problem_title, status, created_at) VALUES (?, ?, ?, ?, ?, 'OPEN', ?)",
        (current_user.email, current_user.name, req.opponent_email.strip().lower(), req.problem_id, p["title"], datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    conn.commit()
    conn.close()
    return {"status": "challenged"}


@router.get("/prep-tracks")
def get_prep_tracks(current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT track, AVG(score) as avg_score, COUNT(*) as attempts FROM prep_assessments WHERE user_email = ? GROUP BY track", (current_user.email,))
    stats = {r["track"]: {"avg_score": round(r["avg_score"], 1), "attempts": r["attempts"]} for r in cur.fetchall()}
    conn.close()
    return stats


@router.post("/prep-tracks")
def save_prep_track(req: PrepSubmitReq, current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO prep_assessments (user_email, track, score, time_taken_sec, created_at) VALUES (?, ?, ?, ?, ?)",
        (current_user.email, req.track, req.score, req.time_taken_sec, datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    conn.commit()
    conn.close()
    return {"status": "saved"}
