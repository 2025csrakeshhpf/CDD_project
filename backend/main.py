import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models import (
    AptitudeQuestion,
    CodingProblem,
    DailyActivity,
    ProgrammingLanguage,
    StudentAchievement,
    StudentActivityLog,
    StudentSubmission,
    Suggestion,
    TestCase,
    User,
    UserDraft,
    get_session_factory,
)
from sandbox_judge import execute_sandboxed_code
from seed_problems import TOPICS_23, seed_database

DB_PATH = str(Path(__file__).resolve().parent / "placement.db")
FRONTEND_INDEX = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
SECRET_KEY = "vtu-placement-training-secret-2026"

SessionLocal = get_session_factory(DB_PATH)

app = FastAPI(title="Placement Training • Gamified Achievement & AI Career Platform")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(plain: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"{salt}${dk.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    if "$" not in stored:
        return plain == stored
    salt, expected = stored.split("$", 1)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return hmac.compare_digest(dk.hex(), expected)


def create_token(email: str, role: str) -> str:
    payload = json.dumps({"email": email, "role": role, "iat": int(datetime.utcnow().timestamp())}).encode("utf-8")
    b64 = base64.urlsafe_b64encode(payload).decode("utf-8")
    sig = hmac.new(SECRET_KEY.encode("utf-8"), b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def decode_token(token: str) -> Dict[str, str]:
    try:
        b64, sig = token.split(".", 1)
        expected = hmac.new(SECRET_KEY.encode("utf-8"), b64.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("Invalid signature")
        return json.loads(base64.urlsafe_b64decode(b64.encode("utf-8")).decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=401, detail="Authentication required. Please log in.")


def get_current_user(authorization: Optional[str] = Header(None)) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required.")
    claims = decode_token(authorization.split(" ", 1)[1].strip())
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == claims["email"]).first()
        if not user:
            raise HTTPException(status_code=401, detail="User not found.")
        return user
    finally:
        db.close()


def init_db():
    global SessionLocal
    SessionLocal = get_session_factory(DB_PATH)

    conn = get_conn()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS video_interviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            company TEXT NOT NULL,
            mode TEXT NOT NULL,
            duration_sec INTEGER DEFAULT 0,
            communication_score REAL DEFAULT 0.0,
            confidence_score REAL DEFAULT 0.0,
            relevance_score REAL DEFAULT 0.0,
            overall_score REAL DEFAULT 0.0,
            verdict TEXT DEFAULT 'FAIL',
            feedback TEXT DEFAULT '',
            transcript_json TEXT DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS aptitude_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            question_id INTEGER NOT NULL,
            topic TEXT NOT NULL,
            selected_option TEXT NOT NULL,
            is_correct INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    db = SessionLocal()
    try:
        if not db.query(User).filter(User.email == "rakesh@vtu.ac.in").first():
            db.add(User(
                name="Rakesh HP",
                usn="4IK25CS161",
                email="rakesh@vtu.ac.in",
                password_hash=hash_password("rakesh123"),
                role="student",
                preferred_language="python",
                daily_goal_problems=5,
            ))
        if not db.query(User).filter(User.email == "admin@vtu.ac.in").first():
            db.add(User(
                name="Placement Officer (Admin)",
                usn="ADMIN001",
                email="admin@vtu.ac.in",
                password_hash=hash_password("admin123"),
                role="admin",
            ))
        db.commit()
        seed_database(db, DB_PATH)
    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/")
def serve_frontend():
    if FRONTEND_INDEX.exists():
        return FileResponse(str(FRONTEND_INDEX))
    return {"status": "ok"}


# -------------------- SCHEMAS --------------------
class RegisterReq(BaseModel):
    full_name: str
    email: str
    password: str
    confirm_password: str
    usn: Optional[str] = "4IK25CS161"


class LoginReq(BaseModel):
    email: str
    password: str


class DraftSaveReq(BaseModel):
    problem_id: int
    language: str
    code: str


class ExecuteCodeReq(BaseModel):
    problem_id: int
    language: str
    code: str
    mode: str = "run"  # "run" | "submit"
    time_taken_sec: int = 30


class AptitudeCheckReq(BaseModel):
    question_id: int
    selected_option: str


class HRInterviewTurnReq(BaseModel):
    company: str
    mode: str
    question_number: int
    student_answer: str


class HRInterviewFinishReq(BaseModel):
    company: str
    mode: str
    duration_sec: int
    transcript: List[Dict[str, str]]


class AIChatReq(BaseModel):
    message: str


class FeedbackReq(BaseModel):
    subject: str
    category: str
    description: str


# -------------------- AUTH --------------------
@app.post("/api/auth/register")
def register(req: RegisterReq):
    email = req.email.strip().lower()
    if len(req.full_name.strip()) < 2:
        raise HTTPException(status_code=400, detail="Full Name is required.")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="Invalid email format.")
    if len(req.password) < 6 or req.password != req.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords must match and be at least 6 characters.")
    db = SessionLocal()
    try:
        if db.query(User).filter(User.email == email).first():
            raise HTTPException(status_code=409, detail="Email already registered.")
        db.add(User(
            name=req.full_name.strip(),
            usn=(req.usn or "4IK25CS161").strip(),
            email=email,
            password_hash=hash_password(req.password),
            role="student",
        ))
        db.commit()
        return {"message": "Account created successfully. Please log in.", "email": email}
    finally:
        db.close()


@app.post("/api/auth/login")
def login(req: LoginReq):
    email = req.email.strip().lower()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user or not verify_password(req.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Incorrect email or password.")
        return {
            "token": create_token(user.email, user.role),
            "user": {
                "name": user.name,
                "usn": user.usn,
                "email": user.email,
                "role": user.role,
                "preferred_language": user.preferred_language,
            },
        }
    finally:
        db.close()


@app.get("/api/auth/me")
def me(current_user: User = Depends(get_current_user)):
    return {
        "name": current_user.name,
        "usn": current_user.usn,
        "email": current_user.email,
        "role": current_user.role,
        "preferred_language": current_user.preferred_language,
    }


# -------------------- STREAKS & ACHIEVEMENT EVALUATION ENGINE --------------------
def _compute_streaks(db: Session, user_email: str) -> Dict[str, int]:
    rows = (
        db.query(DailyActivity)
        .filter(DailyActivity.user_email == user_email, DailyActivity.submissions_count > 0)
        .order_by(DailyActivity.activity_date.asc())
        .all()
    )
    if not rows:
        return {"current_streak": 0, "longest_streak": 0, "active_days": 0}

    dates = sorted({datetime.strptime(r.activity_date, "%Y-%m-%d").date() for r in rows})
    longest = 1
    run = 1
    for i in range(1, len(dates)):
        if (dates[i] - dates[i - 1]).days == 1:
            run += 1
            longest = max(longest, run)
        else:
            run = 1

    today = datetime.now().date()
    yesterday = today - timedelta(days=1)
    if dates[-1] not in (today, yesterday):
        current = 0
    else:
        current = 1
        for i in range(len(dates) - 1, 0, -1):
            if (dates[i] - dates[i - 1]).days == 1:
                current += 1
            else:
                break

    return {"current_streak": current, "longest_streak": longest, "active_days": len(dates)}


def _evaluate_achievements(db: Session, user_email: str) -> List[Dict]:
    subs = (
        db.query(StudentSubmission)
        .filter(StudentSubmission.user_email == user_email, StudentSubmission.mode == "submit")
        .all()
    )
    solved_ids = {s.problem_id for s in subs if s.passed}
    passed_subs = [s for s in subs if s.passed]

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, topic, difficulty FROM coding_problems")
    prob_meta = {r["id"]: (r["topic"], r["difficulty"]) for r in cur.fetchall()}
    conn.close()

    arrays_solved = sum(1 for pid in solved_ids if prob_meta.get(pid, ("", ""))[0] == "Arrays")
    med_solved = sum(1 for pid in solved_ids if prob_meta.get(pid, ("", ""))[1] == "Medium")
    hard_solved = sum(1 for pid in solved_ids if prob_meta.get(pid, ("", ""))[1] == "Hard")
    fast_solves = sum(1 for s in passed_subs if s.time_taken_sec <= 600)
    accuracy = round((len(passed_subs) / len(subs) * 100.0), 1) if subs else 0.0
    streaks = _compute_streaks(db, user_email)

    specs = [
        ("FIRST_STEP", "🚀", "First Step", "Solve your first coding problem.", len(solved_ids), 1),
        ("PROBLEM_SOLVER", "🏆", "Problem Solver", "Solve 10 coding problems.", len(solved_ids), 10),
        ("CONSISTENT_CODER", "🔥", "Consistent Coder", "Practice for 7 consecutive days.", streaks["current_streak"], 7),
        ("ARRAY_MASTER", "🧩", "Array Master", "Solve 10 Array problems.", arrays_solved, 10),
        ("SPEED_CODER", "⚡", "Speed Coder", "Solve a problem within 10 minutes.", fast_solves, 1),
        ("ACCURACY_CHAMPION", "🎯", "Accuracy Champion", "Maintain >= 90% accuracy (min 5 submits).", int(accuracy) if len(subs) >= 5 else 0, 90),
        ("MEDIUM_MASTER", "💎", "Medium Master", "Solve 20 Medium problems.", med_solved, 20),
        ("HARD_CHALLENGER", "👑", "Hard Challenger", "Solve 10 Hard problems.", hard_solved, 10),
    ]

    existing = {
        a.code: a.unlocked_at
        for a in db.query(StudentAchievement).filter(StudentAchievement.user_email == user_email).all()
    }
    newly_unlocked = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    cards = []
    for code, icon, title, desc, cur_val, target_val in specs:
        eligible = cur_val >= target_val
        unlocked_at = existing.get(code)
        if eligible and not unlocked_at:
            db.add(StudentAchievement(user_email=user_email, code=code, unlocked_at=now))
            db.add(StudentActivityLog(user_email=user_email, icon=icon, message=f"Unlocked {title} achievement", created_at=now))
            unlocked_at = now
            newly_unlocked.append({"code": code, "icon": icon, "title": title, "description": desc})

        pct = min(100, int(round((cur_val / target_val) * 100))) if target_val else 0
        cards.append({
            "code": code,
            "icon": icon,
            "title": title,
            "description": desc,
            "current": min(cur_val, target_val),
            "target": target_val,
            "progress_pct": pct,
            "unlocked": bool(unlocked_at),
            "unlocked_at": unlocked_at or "",
            "newly_unlocked": any(n["code"] == code for n in newly_unlocked),
        })

    if newly_unlocked:
        db.commit()
    return cards


# -------------------- DESIGN 3: GAMIFIED ACHIEVEMENT DASHBOARD ENDPOINT --------------------
@app.get("/api/coding/achievement-dashboard")
def get_achievement_dashboard(current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        user_email = current_user.email
        achievements = _evaluate_achievements(db, user_email)
        streaks = _compute_streaks(db, user_email)

        subs = (
            db.query(StudentSubmission)
            .filter(StudentSubmission.user_email == user_email, StudentSubmission.mode == "submit")
            .all()
        )
        attempted_ids = {s.problem_id for s in subs}
        solved_ids = {s.problem_id for s in subs if s.passed}
        passed_subs = [s for s in subs if s.passed]

        conn = get_conn()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM coding_problems")
        total_problems = cur.fetchone()[0] or 1

        # Topic-wise totals & difficulty distribution in single fast SQL query
        cur.execute(
            """
            SELECT topic,
                   COUNT(*) as total_cnt,
                   SUM(CASE WHEN difficulty='Easy' THEN 1 ELSE 0 END) as easy_cnt,
                   SUM(CASE WHEN difficulty='Medium' THEN 1 ELSE 0 END) as med_cnt,
                   SUM(CASE WHEN difficulty='Hard' THEN 1 ELSE 0 END) as hard_cnt
            FROM coding_problems
            GROUP BY topic
            ORDER BY MIN(id) ASC
            """
        )
        topic_rows = cur.fetchall()

        cur.execute("SELECT id, topic, difficulty FROM coding_problems")
        prob_lookup = {r["id"]: (r["topic"], r["difficulty"]) for r in cur.fetchall()}
        conn.close()

        easy_solved = sum(1 for pid in solved_ids if prob_lookup.get(pid, ("", ""))[1] == "Easy")
        med_solved = sum(1 for pid in solved_ids if prob_lookup.get(pid, ("", ""))[1] == "Medium")
        hard_solved = sum(1 for pid in solved_ids if prob_lookup.get(pid, ("", ""))[1] == "Hard")

        solved_by_topic: Dict[str, int] = {}
        attempts_by_topic: Dict[str, int] = {}
        passes_by_topic: Dict[str, int] = {}

        for pid in solved_ids:
            tp = prob_lookup.get(pid, ("", ""))[0]
            if tp:
                solved_by_topic[tp] = solved_by_topic.get(tp, 0) + 1

        for s in subs:
            tp = prob_lookup.get(s.problem_id, ("", ""))[0]
            if tp:
                attempts_by_topic[tp] = attempts_by_topic.get(tp, 0) + 1
                if s.passed:
                    passes_by_topic[tp] = passes_by_topic.get(tp, 0) + 1

        topic_cards = []
        for r in topic_rows:
            tp = r["topic"]
            tot = r["total_cnt"]
            slv = solved_by_topic.get(tp, 0)
            pct = round((slv / tot) * 100.0, 1) if tot else 0.0
            topic_cards.append({
                "topic": tp,
                "total": tot,
                "solved": slv,
                "completion_pct": pct,
                "mastered": slv >= tot and tot > 0,
                "difficulty_dist": {
                    "Easy": r["easy_cnt"],
                    "Medium": r["med_cnt"],
                    "Hard": r["hard_cnt"],
                },
            })

        # 10-Axis Skill Radar Chart
        radar_axes = [
            ("Arrays", "Arrays"),
            ("Strings", "Strings"),
            ("Searching", "Searching"),
            ("Sorting", "Sorting"),
            ("Recursion", "Recursion"),
            ("Dynamic Programming", "Dynamic Programming"),
            ("Trees", "Trees"),
            ("Graphs", "Graphs"),
            ("SQL", "Database and SQL"),
            ("OOP", "Object-Oriented Programming"),
        ]
        radar_data = []
        for label, db_topic in radar_axes:
            att = attempts_by_topic.get(db_topic, 0)
            pas = passes_by_topic.get(db_topic, 0)
            slv = solved_by_topic.get(db_topic, 0)
            if att == 0:
                score = 0
            else:
                acc_part = (pas / att) * 65.0
                vol_part = min(35.0, slv * 7.0)
                score = min(100, int(round(acc_part + vol_part)))
            radar_data.append({"label": label, "topic": db_topic, "score": score, "solved": slv, "attempts": att})

        # Daily activity & 30-day green intensity heatmap
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_act = db.query(DailyActivity).filter(DailyActivity.user_email == user_email, DailyActivity.activity_date == today_str).first()
        today_solved = today_act.problems_solved if today_act else 0

        all_acts = db.query(DailyActivity).filter(DailyActivity.user_email == user_email).all()
        act_map = {a.activity_date: a for a in all_acts}
        total_practice_mins = round(sum(a.practice_seconds for a in all_acts) / 60.0, 1)

        heatmap = []
        for offset in range(29, -1, -1):
            d_obj = datetime.now().date() - timedelta(days=offset)
            d_str = d_obj.strftime("%Y-%m-%d")
            rec = act_map.get(d_str)
            cnt = rec.problems_solved if rec else 0
            mins = round((rec.practice_seconds if rec else 0) / 60.0, 1)
            level = 0 if cnt == 0 else (1 if cnt == 1 else (2 if cnt <= 3 else 3))
            heatmap.append({"date": d_str, "solved": cnt, "minutes": mins, "level": level})

        # Dynamic AI Performance Recommendations from real database stats
        accuracy = round((len(passed_subs) / len(subs) * 100.0), 1) if subs else 0.0
        avg_sec = round(sum(s.time_taken_sec for s in subs) / len(subs), 1) if subs else 0.0
        ai_recs = []

        weak_topic = next((r["topic"] for r in radar_data if r["attempts"] > 0 and r["score"] < 55), None)
        strong_topic = next((r["topic"] for r in radar_data if r["solved"] >= 2 and r["score"] >= 70), None)

        if strong_topic:
            ai_recs.append(f"Your {strong_topic} performance is improving ({solved_by_topic.get(strong_topic, 0)} solved). Start practicing Medium-level questions.")
        if weak_topic:
            ai_recs.append(f"You are struggling with {weak_topic}. Revise the basics and solve 5 Easy {weak_topic} problems.")
        if avg_sec > 0 and avg_sec <= 600:
            ai_recs.append(f"Your coding speed is strong (avg {round(avg_sec/60, 1)} mins/problem). Challenge yourself with Medium & Hard problems.")
        elif avg_sec > 600:
            ai_recs.append("Your solving time exceeds 10 minutes on average. Try solving Easy problems within 20 minutes.")
        if not ai_recs:
            ai_recs.append("Solve your first Easy problem in Arrays or Strings to unlock the First Step achievement and populate your Skill Radar.")

        # Recent Activity Feed
        logs = (
            db.query(StudentActivityLog)
            .filter(StudentActivityLog.user_email == user_email)
            .order_by(StudentActivityLog.id.desc())
            .limit(8)
            .all()
        )
        recent_activity = [{"icon": l.icon, "message": l.message, "time": l.created_at} for l in logs]

        overall_pct = round((len(solved_ids) / total_problems) * 100.0, 2)

        return {
            "student": {
                "name": current_user.name,
                "usn": current_user.usn,
                "email": current_user.email,
            },
            "overview": {
                "total_problems": total_problems,
                "solved_problems": len(solved_ids),
                "remaining_problems": max(0, total_problems - len(solved_ids)),
                "attempted_problems": len(attempted_ids),
                "completion_pct": overall_pct,
                "accuracy_pct": accuracy,
                "easy_solved": easy_solved,
                "medium_solved": med_solved,
                "hard_solved": hard_solved,
                "current_streak": streaks["current_streak"],
                "longest_streak": streaks["longest_streak"],
                "active_days": streaks["active_days"],
                "daily_goal_done": today_solved,
                "daily_goal_target": current_user.daily_goal_problems or 5,
                "total_practice_mins": total_practice_mins,
            },
            "achievements": achievements,
            "radar": radar_data,
            "topics": topic_cards,
            "heatmap": heatmap,
            "ai_recommendations": ai_recs,
            "recent_activity": recent_activity,
        }
    finally:
        db.close()


# -------------------- CODING PROBLEMS & MULTI-LANGUAGE EXECUTION --------------------
@app.get("/api/languages")
def get_supported_languages(current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        # Return all 15 programming languages with their runtime availability metadata
        langs = db.query(ProgrammingLanguage).order_by(ProgrammingLanguage.id.asc()).all()
        return [
            {
                "slug": l.slug,
                "display_name": l.display_name,
                "is_supported": l.is_supported,
                "runtime_info": l.runtime_info,
            }
            for l in langs
        ]
    finally:
        db.close()


@app.get("/api/coding/problems")
def list_coding_problems(
    topic: Optional[str] = "Arrays",
    difficulty: Optional[str] = "All",
    company: Optional[str] = "All",
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        user_subs = (
            db.query(StudentSubmission.problem_id, StudentSubmission.passed)
            .filter(StudentSubmission.user_email == current_user.email, StudentSubmission.mode == "submit")
            .all()
        )
        solved_ids = {pid for pid, passed in user_subs if passed}
        attempted_ids = {pid for pid, _ in user_subs}

        q = db.query(CodingProblem)
        if topic and topic != "All":
            q = q.filter(CodingProblem.topic == topic)
        if difficulty and difficulty != "All":
            q = q.filter(CodingProblem.difficulty == difficulty)
        if company and company != "All":
            q = q.filter(CodingProblem.company == company)
        if search and search.strip():
            term = f"%{search.strip()}%"
            q = q.filter((CodingProblem.title.ilike(term)) | (CodingProblem.topic.ilike(term)))

        filtered_total = q.count()
        page_items = q.order_by(CodingProblem.problem_number.asc()).offset(offset).limit(limit).all()

        return {
            "filtered_total": filtered_total,
            "topics": TOPICS_23,
            "problems": [
                {
                    "id": p.id,
                    "problem_number": p.problem_number,
                    "title": p.title,
                    "difficulty": p.difficulty,
                    "topic": p.topic,
                    "company": p.company,
                    "status": "Solved" if p.id in solved_ids else ("Attempted" if p.id in attempted_ids else "Unsolved"),
                }
                for p in page_items
            ],
        }
    finally:
        db.close()


@app.get("/api/coding/problems/{problem_id}")
def get_coding_problem_workspace(problem_id: int, current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        prob = db.query(CodingProblem).filter(CodingProblem.id == problem_id).first()
        if not prob:
            raise HTTPException(status_code=404, detail="Problem not found.")

        sample_cases = db.query(TestCase).filter(TestCase.problem_id == prob.id, TestCase.is_sample == True).all()
        total_cases = db.query(TestCase).filter(TestCase.problem_id == prob.id).count()
        is_solved = (
            db.query(StudentSubmission)
            .filter(StudentSubmission.user_email == current_user.email, StudentSubmission.problem_id == prob.id, StudentSubmission.passed == True)
            .count()
            > 0
        )

        starters = json.loads(prob.starters_json or "{}")
        user_drafts = db.query(UserDraft).filter(UserDraft.user_email == current_user.email, UserDraft.problem_id == prob.id).all()
        draft_map = dict(starters)
        for d in user_drafts:
            draft_map[d.language] = d.code

        return {
            "id": prob.id,
            "problem_number": prob.problem_number,
            "title": prob.title,
            "topic": prob.topic,
            "difficulty": prob.difficulty,
            "company": prob.company,
            "description": prob.description,
            "input_format": prob.input_format,
            "output_format": prob.output_format,
            "constraints": prob.constraints,
            "hints": json.loads(prob.hints_json or "[]"),
            "sample_cases": [{"input": c.input_data, "expected": c.expected_output} for c in sample_cases],
            "total_test_cases": total_cases,
            "is_solved": is_solved,
            "preferred_language": current_user.preferred_language,
            "drafts_by_language": draft_map,
            "starters_by_language": starters,
        }
    finally:
        db.close()


@app.post("/api/coding/draft")
def save_language_draft(req: DraftSaveReq, current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == current_user.email).first()
        if user:
            user.preferred_language = req.language
        draft = db.query(UserDraft).filter(UserDraft.user_email == current_user.email, UserDraft.problem_id == req.problem_id, UserDraft.language == req.language).first()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if draft:
            draft.code = req.code
            draft.updated_at = now
        else:
            db.add(UserDraft(user_email=current_user.email, problem_id=req.problem_id, language=req.language, code=req.code, updated_at=now))
        db.commit()
        return {"status": "saved"}
    finally:
        db.close()


@app.post("/api/coding/execute")
def execute_coding_endpoint(req: ExecuteCodeReq, current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        prob = db.query(CodingProblem).filter(CodingProblem.id == req.problem_id).first()
        if not prob:
            raise HTTPException(status_code=404, detail="Problem not found.")

        is_submit = req.mode.strip().lower() == "submit"
        q_cases = db.query(TestCase).filter(TestCase.problem_id == prob.id)
        if not is_submit:
            q_cases = q_cases.filter(TestCase.is_sample == True)
        cases = [{"input": c.input_data, "expected": c.expected_output, "is_sample": c.is_sample} for c in q_cases.all()]

        judge = execute_sandboxed_code(req.language, req.code, cases)
        all_passed = (judge["verdict"] == "ACCEPTED") and (judge["passed_cases"] == judge["total_cases"]) and (judge["total_cases"] > 0)
        # Never mark solved on Run Code; only on Submit Code when ALL cases pass
        marked_solved = is_submit and all_passed

        now_full = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today_str = datetime.now().strftime("%Y-%m-%d")

        newly_unlocked = []
        if is_submit:
            already_solved = (
                db.query(StudentSubmission)
                .filter(StudentSubmission.user_email == current_user.email, StudentSubmission.problem_id == prob.id, StudentSubmission.passed == True)
                .count()
                > 0
            )

            db.add(StudentSubmission(
                user_email=current_user.email,
                problem_id=prob.id,
                language=req.language,
                mode="submit",
                source_code=req.code,
                verdict=judge["verdict"],
                passed=marked_solved,
                passed_cases=judge["passed_cases"],
                total_cases=judge["total_cases"],
                execution_time_ms=judge["execution_time_ms"],
                time_taken_sec=max(1, req.time_taken_sec),
                error_message=judge["error_message"],
                submitted_at=now_full,
            ))

            act = db.query(DailyActivity).filter(DailyActivity.user_email == current_user.email, DailyActivity.activity_date == today_str).first()
            if not act:
                act = DailyActivity(user_email=current_user.email, activity_date=today_str, problems_solved=0, submissions_count=0, practice_seconds=0)
                db.add(act)
            act.submissions_count += 1
            act.practice_seconds += max(5, req.time_taken_sec)
            if marked_solved and not already_solved:
                act.problems_solved += 1

            log_msg = f"Solved {prob.title} ({prob.topic})" if marked_solved else f"Attempted {prob.title} ({judge['verdict']})"
            db.add(StudentActivityLog(
                user_email=current_user.email,
                icon="✅" if marked_solved else "🟡",
                message=log_msg,
                created_at=now_full,
            ))
            db.commit()

            all_ach = _evaluate_achievements(db, current_user.email)
            newly_unlocked = [a for a in all_ach if a.get("newly_unlocked")]

        return {
            "mode": "submit" if is_submit else "run",
            "verdict": judge["verdict"],
            "marked_solved": marked_solved,
            "total_cases": judge["total_cases"],
            "passed_cases": judge["passed_cases"],
            "failed_cases": judge["failed_cases"],
            "execution_time_ms": judge["execution_time_ms"],
            "error_message": judge["error_message"],
            "case_results": judge["case_results"],
            "newly_unlocked": newly_unlocked,
        }
    finally:
        db.close()


# -------------------- APTITUDE, HR VIDEO INTERVIEW, AI SUGGESTIONS & FEEDBACK --------------------
@app.get("/api/aptitude/questions")
def list_aptitude_questions(
    topic: Optional[str] = "All",
    difficulty: Optional[str] = "All",
    limit: int = 20,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
):
    db = SessionLocal()
    try:
        q = db.query(AptitudeQuestion)
        if topic and topic != "All":
            q = q.filter(AptitudeQuestion.topic == topic)
        if difficulty and difficulty != "All":
            q = q.filter(AptitudeQuestion.difficulty == difficulty)
        total = q.count()
        items = q.order_by(AptitudeQuestion.question_number.asc()).offset(offset).limit(limit).all()
        return {
            "total": total,
            "questions": [
                {
                    "id": item.id,
                    "question_number": item.question_number,
                    "topic": item.topic,
                    "difficulty": item.difficulty,
                    "company": item.company,
                    "question_text": item.question_text,
                    "options": json.loads(item.options_json),
                }
                for item in items
            ],
        }
    finally:
        db.close()


@app.post("/api/aptitude/check")
def check_aptitude_answer(req: AptitudeCheckReq, current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        q = db.query(AptitudeQuestion).filter(AptitudeQuestion.id == req.question_id).first()
        if not q:
            raise HTTPException(status_code=404, detail="Aptitude question not found.")
        is_correct = req.selected_option.strip() == q.correct_option.strip()
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO aptitude_attempts (user_email, question_id, topic, selected_option, is_correct, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (current_user.email, q.id, q.topic, req.selected_option, 1 if is_correct else 0, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()
        conn.close()
        return {
            "is_correct": is_correct,
            "banner": "Your answer is correct" if is_correct else "Your answer is wrong",
            "correct_option": q.correct_option,
            "solution": q.solution_text,
        }
    finally:
        db.close()


HR_QUESTIONS = [
    "Tell me about yourself, your academic background at VTU, and what motivates you as an engineer.",
    "What are your top two technical strengths, and how have you applied them in a real project?",
    "Tell me about a time you faced a tight deadline or difficult bug. How did you resolve it?",
    "Where do you see yourself growing over the next two years, and why should we select you?",
    "Give an example showing how you collaborate and adapt in fast-paced engineering teams."
]


@app.post("/api/video-interview/turn")
def hr_video_interview_turn(req: HRInterviewTurnReq, current_user: User = Depends(get_current_user)):
    words = req.student_answer.strip().split()
    q_idx = req.question_number
    ack = "Thank you for that clear response." if len(words) >= 8 else "Try elaborating a bit more with concrete examples."
    if q_idx < len(HR_QUESTIONS):
        return {"ai_reply": f"{ack} Next HR Question ({q_idx + 1} of {len(HR_QUESTIONS)}): {HR_QUESTIONS[q_idx]}", "completed": False}
    return {"ai_reply": f"{ack} All {len(HR_QUESTIONS)} HR questions completed. Click 'End & Get Pass/Fail Result'.", "completed": True}


@app.post("/api/video-interview/finish")
def finish_hr_video_interview(req: HRInterviewFinishReq, current_user: User = Depends(get_current_user)):
    student_answers = [t.get("text", "").strip() for t in req.transcript if t.get("speaker") == "Student" and t.get("text", "").strip()]
    answered_count = len(student_answers)
    total_words = sum(len(a.split()) for a in student_answers)
    joined = " ".join(student_answers).lower()
    hits = sum(1 for w in ["project", "team", "learn", "problem", "skill", "develop", "experience", "challenge", "solution", "goal"] if w in joined)

    comm = min(98.0, round(35.0 + min(total_words, 160) * 0.35, 1))
    conf = min(98.0, round(40.0 + answered_count * 10.0 + hits * 2.0, 1))
    rel = min(98.0, round(38.0 + hits * 6.5, 1))
    overall = round(comm * 0.35 + conf * 0.35 + rel * 0.30, 1)
    passed = (answered_count >= 3) and (overall >= 65.0)
    verdict = "PASS" if passed else "FAIL"
    feedback = (
        f"Congratulations! You PASSED the {req.company} HR Video Interview ({overall}%)."
        if passed
        else f"Result: FAILED ({overall}% — minimum 65% and 3 structured answers required)."
    )

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO video_interviews (
            user_email, company, mode, duration_sec, communication_score, confidence_score,
            relevance_score, overall_score, verdict, feedback, transcript_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (current_user.email, req.company, req.mode, req.duration_sec, comm, conf, rel, overall, verdict, feedback, json.dumps(req.transcript), datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    conn.commit()
    conn.close()
    return {"verdict": verdict, "passed": passed, "overall_score": overall, "communication_score": comm, "confidence_score": conf, "relevance_score": rel, "feedback": feedback}


@app.get("/api/ai/suggestions")
def get_ai_suggestions(current_user: User = Depends(get_current_user)):
    dash = get_achievement_dashboard(current_user)
    ov = dash["overview"]
    return {
        "recommendations": [{"category": "AI Performance Coach", "title": "Personalized Action", "advice": r} for r in dash["ai_recommendations"]],
        "learning_plan": [
            {"day": "Day 1", "focus": "Arrays – 5 Easy Problems"},
            {"day": "Day 2", "focus": "Strings & Two Pointers – 5 Problems"},
            {"day": "Day 3", "focus": "Searching & Sorting – 5 Problems"},
            {"day": "Day 4", "focus": "Aptitude & Logical Reasoning – 20 Questions"},
            {"day": "Day 5", "focus": "HR AI Video Mock Interview"},
        ],
        "overview": ov,
    }


@app.post("/api/ai/chat")
def ai_chat(req: AIChatReq, current_user: User = Depends(get_current_user)):
    dash = get_achievement_dashboard(current_user)
    ov = dash["overview"]
    return {
        "reply": (
            f"Hi {current_user.name}! You have solved {ov['solved_problems']} of {ov['total_problems']} problems "
            f"({ov['easy_solved']} Easy, {ov['medium_solved']} Medium, {ov['hard_solved']} Hard) with a {ov['current_streak']}-day streak. "
            f"Top recommendation: {dash['ai_recommendations'][0]}"
        )
    }


@app.post("/api/feedback")
def submit_feedback(req: FeedbackReq, current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        db.add(Suggestion(
            user_email=current_user.email,
            student_name=f"{current_user.name} ({current_user.usn})",
            subject=req.subject.strip(),
            category=req.category,
            description=req.description.strip(),
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        ))
        db.commit()
        return {"status": "saved"}
    finally:
        db.close()


@app.get("/api/feedback/my")
def list_feedback(current_user: User = Depends(get_current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Suggestion).filter(Suggestion.user_email == current_user.email).order_by(Suggestion.id.desc()).all()
        return [{"subject": r.subject, "category": r.category, "description": r.description, "status": r.status} for r in rows]
    finally:
        db.close()

from competitive_extensions import router as competitive_router
app.include_router(competitive_router)


from duel_service import router as duel_router
app.include_router(duel_router)


from invitation_service import router as invitation_router
app.include_router(invitation_router)


@app.get("/health")
def health_check():
    return {"status": "ok"}
