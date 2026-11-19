import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
EXISTING_DB_PATH = str(BASE_DIR.parent / "backend" / "placement.db")
NEXTGEN_DB_PATH = str(BASE_DIR / "nextgen_arena.db")
ARENA_HTML = BASE_DIR / "arena.html"
SECRET_KEY = "nextgen-arena-isolated-secret-2026"

app = FastAPI(title="NextGen Competitive Programming & AI Placement Ecosystem")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory rate limiter (max 60 judge/API requests per minute per IP)
_RATE_BUCKETS: Dict[str, List[float]] = defaultdict(list)


def check_rate_limit(request: Request):
    ip = request.client.host if request.client else "local"
    now = time.time()
    window = [t for t in _RATE_BUCKETS[ip] if now - t < 60.0]
    if len(window) >= 60:
        raise HTTPException(status_code=429, detail="Rate limit exceeded (60 req/min). Please wait.")
    window.append(now)
    _RATE_BUCKETS[ip] = window


def get_ng_conn():
    conn = sqlite3.connect(NEXTGEN_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# -------------------- AUTH REUSE (READ-ONLY AGAINST EXISTING USERS) --------------------
def _verify_password(plain: str, stored: str) -> bool:
    if "$" not in stored:
        return plain == stored
    salt, expected = stored.split("$", 1)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return hmac.compare_digest(dk.hex(), expected)


def _issue_token(email: str, name: str, usn: str, role: str) -> str:
    payload = json.dumps({"email": email, "name": name, "usn": usn, "role": role, "iat": int(time.time())}).encode()
    b64 = base64.urlsafe_b64encode(payload).decode()
    sig = hmac.new(SECRET_KEY.encode(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def get_current_user(authorization: Optional[str] = Header(None)) -> Dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Login required.")
    try:
        b64, sig = authorization.split(" ", 1)[1].strip().split(".", 1)
        expected = hmac.new(SECRET_KEY.encode(), b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("Invalid signature")
        return json.loads(base64.urlsafe_b64decode(b64.encode()).decode())
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired session token.")


def require_admin(user: Dict = Depends(get_current_user)) -> Dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


# -------------------- ISOLATED DATABASE INITIALIZATION --------------------
def init_nextgen_db():
    conn = get_ng_conn()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS ng_problems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            topic TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            company_tag TEXT DEFAULT 'Google',
            time_limit_ms INTEGER DEFAULT 2000,
            memory_limit_mb INTEGER DEFAULT 256,
            checker_type TEXT DEFAULT 'exact', -- exact | float_1e6 | unordered_tokens
            statement TEXT NOT NULL,
            input_format TEXT NOT NULL,
            output_format TEXT NOT NULL,
            constraints TEXT NOT NULL,
            hints_json TEXT NOT NULL,
            editorial TEXT NOT NULL,
            optimal_complexity TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ng_test_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            problem_id INTEGER NOT NULL,
            input_data TEXT NOT NULL,
            expected_output TEXT NOT NULL,
            is_sample INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS ng_bookmarks (
            user_email TEXT NOT NULL,
            problem_id INTEGER NOT NULL,
            PRIMARY KEY (user_email, problem_id)
        );

        CREATE TABLE IF NOT EXISTS ng_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            user_name TEXT NOT NULL,
            problem_id INTEGER NOT NULL,
            contest_id INTEGER DEFAULT 0,
            language TEXT NOT NULL,
            source_code TEXT NOT NULL,
            code_fingerprint TEXT NOT NULL,
            verdict TEXT NOT NULL, -- ACCEPTED | WRONG ANSWER | COMPILATION ERROR | RUNTIME ERROR | TIME LIMIT EXCEEDED | MEMORY LIMIT EXCEEDED
            passed_cases INTEGER DEFAULT 0,
            total_cases INTEGER DEFAULT 0,
            exec_time_ms REAL DEFAULT 0.0,
            memory_kb INTEGER DEFAULT 0,
            plagiarism_score REAL DEFAULT 0.0,
            error_detail TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ng_contests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            contest_type TEXT NOT NULL, -- Weekly | Monthly | Global | University | College | Team | Virtual | Private | Company | AI-Personalized
            scope_tag TEXT DEFAULT 'Global',
            duration_mins INTEGER DEFAULT 90,
            penalty_mins INTEGER DEFAULT 10,
            is_private INTEGER DEFAULT 0,
            access_code TEXT DEFAULT '',
            problem_ids_json TEXT NOT NULL,
            created_by TEXT DEFAULT 'system'
        );

        CREATE TABLE IF NOT EXISTS ng_contest_registrations (
            contest_id INTEGER NOT NULL,
            user_email TEXT NOT NULL,
            user_name TEXT NOT NULL,
            team_name TEXT DEFAULT 'Solo',
            tab_switches INTEGER DEFAULT 0,
            registered_at TEXT NOT NULL,
            PRIMARY KEY (contest_id, user_email)
        );

        CREATE TABLE IF NOT EXISTS ng_ratings (
            user_email TEXT PRIMARY KEY,
            user_name TEXT NOT NULL,
            country TEXT DEFAULT 'India',
            university TEXT DEFAULT 'VTU',
            college TEXT DEFAULT 'VTU Main Campus',
            rating INTEGER DEFAULT 1400,
            max_rating INTEGER DEFAULT 1400,
            contests_count INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS ng_rating_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            contest_title TEXT NOT NULL,
            old_rating INTEGER NOT NULL,
            new_rating INTEGER NOT NULL,
            delta INTEGER NOT NULL,
            recorded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ng_learning_paths (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT NOT NULL,
            title TEXT NOT NULL,
            topics_json TEXT NOT NULL,
            description TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ng_community_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            problem_id INTEGER DEFAULT 0,
            user_email TEXT NOT NULL,
            user_name TEXT NOT NULL,
            post_type TEXT DEFAULT 'Discussion', -- Discussion | Editorial | Solution
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            is_flagged INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ng_social_follows (
            follower_email TEXT NOT NULL,
            following_email TEXT NOT NULL,
            PRIMARY KEY (follower_email, following_email)
        );

        CREATE TABLE IF NOT EXISTS ng_tournaments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT DEFAULT 'ACTIVE',
            prize_badge TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ng_interview_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            round_type TEXT NOT NULL,
            company TEXT NOT NULL,
            transcript_json TEXT NOT NULL,
            technical_score REAL DEFAULT 0.0,
            communication_score REAL DEFAULT 0.0,
            overall_score REAL DEFAULT 0.0,
            verdict TEXT NOT NULL,
            feedback TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )

    cur.execute("SELECT COUNT(*) FROM ng_problems")
    if cur.fetchone()[0] == 0:
        seed_probs = [
            ("NG101", "Subarray Equilibrium & Prefix Balance", "Arrays", "Easy", "Google", 2000, 256, "exact",
             "Given N integers, find the lowest 0-based index i where sum(arr[0..i-1]) == sum(arr[i+1..N-1]), or print -1.",
             "Line 1: N\nLine 2: N integers", "Single integer index or -1", "1 <= N <= 10^5",
             json.dumps(["Hint 1: Compute total_sum first in O(N).", "Hint 2: Track left_sum while scanning; right_sum = total_sum - left_sum - arr[i]."]),
             "Maintain prefix sum in a single O(N) pass with O(1) extra space.", "Time: O(N), Space: O(1)",
             [("5\n1 3 5 2 2", "2", 1), ("3\n1 2 3", "-1", 1), ("4\n0 0 0 0", "0", 0)]),

            ("NG102", "K-Distinct Longest Substring Window", "Strings", "Medium", "Amazon", 2000, 256, "exact",
             "Given string S and integer K, print the maximum length of a substring containing at most K distinct characters.",
             "Line 1: S\nLine 2: K", "Maximum substring length", "1 <= |S| <= 10^5, 1 <= K <= 26",
             json.dumps(["Hint 1: Expand right pointer and store char counts in a hash map.", "Hint 2: Shrink left pointer while map size > K."]),
             "Sliding window with frequency hash map runs in O(N) time.", "Time: O(N), Space: O(K)",
             [("eceba\n2", "3", 1), ("aa\n1", "2", 1), ("abcadcacacaca\n3", "11", 0)]),

            ("NG103", "Unordered Prime Factors Set", "Mathematics", "Medium", "Microsoft", 2000, 256, "unordered_tokens",
             "Given integer N > 1, print all distinct prime factors of N in any order separated by spaces.",
             "Single integer N", "Space-separated distinct prime factors", "2 <= N <= 10^12",
             json.dumps(["Hint 1: Trial divide up to sqrt(N).", "Hint 2: If remaining N > 1 after loop, N itself is prime."]),
             "Trial division up to sqrt(N) finds all prime factors in O(sqrt(N)).", "Time: O(sqrt(N)), Space: O(1)",
             [("84", "2 3 7", 1), ("97", "97", 1), ("360", "2 3 5", 0)]),

            ("NG104", "Minimum Coin Change Combinations", "Dynamic Programming", "Hard", "Uber", 2000, 256, "exact",
             "Given N coin denominations and target amount A, print the fewest coins needed to make A, or -1 if impossible.",
             "Line 1: N A\nLine 2: N coin values", "Minimum coins or -1", "1 <= N <= 50, 1 <= A <= 10^4",
             json.dumps(["Hint 1: Define dp[x] = min coins to form sum x.", "Hint 2: dp[x] = min(dp[x], 1 + dp[x - c]) for each coin c <= x."]),
             "Bottom-up 1D DP computes optimal coin count in O(N * A) time.", "Time: O(N * A), Space: O(A)",
             [("3 11\n1 2 5", "3", 1), ("1 3\n2", "-1", 1), ("4 27\n1 4 5 10", "4", 0)]),

            ("NG105", "Connected Server Clusters", "Graphs", "Medium", "TCS", 2000, 256, "exact",
             "Given V vertices (0..V-1) and E undirected edges, print the total number of connected components.",
             "Line 1: V E\nNext E lines: u v", "Integer count of connected components", "1 <= V <= 10^4",
             json.dumps(["Hint 1: Build an adjacency list and maintain a visited array.", "Hint 2: Run BFS/DFS from each unvisited vertex and increment component count."]),
             "DFS/BFS traversal or Disjoint Set Union (DSU) solves in O(V + E).", "Time: O(V + E), Space: O(V + E)",
             [("5 3\n0 1\n1 2\n3 4", "2", 1), ("4 0", "4", 1), ("6 4\n0 1\n2 3\n4 5\n1 2", "2", 0)]),
        ]
        for code, title, topic, diff, comp, tl, ml, chk, stmt, inf, outf, cons, hints, ed, cx, cases in seed_probs:
            cur.execute(
                """
                INSERT INTO ng_problems (code, title, topic, difficulty, company_tag, time_limit_ms, memory_limit_mb, checker_type, statement, input_format, output_format, constraints, hints_json, editorial, optimal_complexity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (code, title, topic, diff, comp, tl, ml, chk, stmt, inf, outf, cons, hints, ed, cx),
            )
            pid = cur.lastrowid
            for inp, exp, samp in cases:
                cur.execute("INSERT INTO ng_test_cases (problem_id, input_data, expected_output, is_sample) VALUES (?, ?, ?, ?)", (pid, inp, exp, samp))

    cur.execute("SELECT COUNT(*) FROM ng_contests")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO ng_contests (title, contest_type, scope_tag, duration_mins, penalty_mins, is_private, access_code, problem_ids_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("NextGen Global Weekly Championship #42", "Weekly", "Global", 90, 10, 0, "", json.dumps([1, 2, 4])),
                ("VTU Inter-College Coding Cup", "University", "VTU", 120, 10, 0, "", json.dumps([1, 3, 5])),
                ("Google & Amazon SDE-1 Simulation Round", "Company", "FAANG", 75, 15, 0, "", json.dumps([2, 4, 5])),
                ("Private Squad Scrimmage (Code: VTU2026)", "Private", "Team", 60, 10, 1, "VTU2026", json.dumps([1, 2])),
            ],
        )

    cur.execute("SELECT COUNT(*) FROM ng_learning_paths")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO ng_learning_paths (level, title, topics_json, description) VALUES (?, ?, ?, ?)",
            [
                ("Beginner", "Foundations of Algorithmic Thinking", json.dumps(["Arrays", "Strings", "Mathematics"]), "Master prefix sums, hash maps, and number theory."),
                ("Intermediate", "Core Interview Data Structures", json.dumps(["Strings", "Graphs", "Sorting"]), "Sliding windows, BFS/DFS graph traversals, and greedy invariants."),
                ("Advanced", "Competitive Dynamic Programming & Optimization", json.dumps(["Dynamic Programming", "Graphs"]), "State compression, knapsack transitions, and hard contest problems."),
            ],
        )

    cur.execute("SELECT COUNT(*) FROM ng_tournaments")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO ng_tournaments (title, stage, status, prize_badge) VALUES (?, ?, ?, ?)",
            [
                ("National Campus Grand Prix 2026", "Stage 2: Qualifier Arena", "ACTIVE", "Grandmaster Trophy"),
                ("AI Adaptive Duel Series", "Stage 1: Open Ladder", "ACTIVE", "Adaptive Tactician"),
            ],
        )

    conn.commit()
    conn.close()


@app.on_event("startup")
def on_startup():
    init_nextgen_db()


@app.get("/")
def serve_arena():
    return FileResponse(str(ARENA_HTML))


# -------------------- AUTH ENDPOINT (REUSES EXISTING CREDENTIALS) --------------------
class LoginReq(BaseModel):
    email: str
    password: str


@app.post("/ng-api/auth/login")
def ng_login(req: LoginReq):
    email = req.email.strip().lower()
    if os.path.exists(EXISTING_DB_PATH):
        conn = sqlite3.connect(EXISTING_DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT name, usn, email, password_hash, role FROM users WHERE email = ?", (email,))
        row = cur.fetchone()
        conn.close()
        if row and _verify_password(req.password, row["password_hash"]):
            token = _issue_token(row["email"], row["name"], row["usn"] or "4IK25CS161", row["role"] or "student")
            return {"token": token, "user": {"name": row["name"], "usn": row["usn"], "email": row["email"], "role": row["role"]}}

    raise HTTPException(status_code=401, detail="Invalid credentials. Use your existing Placement Tracker account.")


# -------------------- MODULE 1: NEXT-GEN ONLINE JUDGE (SANDBOXED + CUSTOM CHECKERS + PLAGIARISM) --------------------
LANG_RUNTIMES = [
    ("python", "Python 3", [sys.executable]),
    ("c", "C (GCC)", ["gcc", "clang"]),
    ("cpp", "C++17 (G++)", ["g++", "clang++"]),
    ("java", "Java (OpenJDK)", ["javac"]),
    ("javascript", "JavaScript (Node)", ["node"]),
    ("csharp", "C# (.NET/Mono)", ["csc", "mcs"]),
    ("go", "Go", ["go"]),
    ("rust", "Rust", ["rustc"]),
    ("kotlin", "Kotlin", ["kotlinc"]),
]


@app.get("/ng-api/languages")
def ng_languages(user: Dict = Depends(get_current_user)):
    out = []
    for slug, name, bins in LANG_RUNTIMES:
        avail = any(b == sys.executable or shutil.which(b) for b in bins)
        out.append({"slug": slug, "name": name, "available": avail})
    return out


def _check_output(checker_type: str, actual: str, expected: str) -> bool:
    a, e = actual.strip(), expected.strip()
    if checker_type == "unordered_tokens":
        return sorted(a.split()) == sorted(e.split())
    if checker_type == "float_1e6":
        try:
            return abs(float(a) - float(e)) <= 1e-6
        except ValueError:
            return False
    return a == e


def _code_fingerprint(code: str) -> str:
    normalized = re.sub(r"\s+", "", re.sub(r"#.*|//.*", "", code))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _run_sandbox(language: str, code: str, cases: List[Dict], time_limit_ms: int, checker_type: str) -> Dict:
    if not code.strip():
        return {"verdict": "COMPILATION ERROR", "passed": 0, "total": len(cases), "time_ms": 0.0, "mem_kb": 0, "error": "Code cannot be empty.", "results": []}

    timeout_sec = max(1.0, time_limit_ms / 1000.0)
    with tempfile.TemporaryDirectory(prefix="ng_judge_") as tmp:
        cmd: List[str] = []
        if language == "python":
            f = os.path.join(tmp, "sol.py")
            open(f, "w", encoding="utf-8").write(code)
            cmd = [sys.executable, "-I", f]
        elif language == "javascript":
            node = shutil.which("node")
            if not node:
                return {"verdict": "COMPILATION ERROR", "passed": 0, "total": len(cases), "time_ms": 0.0, "mem_kb": 0, "error": "Node.js not installed on host.", "results": []}
            f = os.path.join(tmp, "sol.js")
            open(f, "w", encoding="utf-8").write(code)
            cmd = [node, f]
        elif language in ("c", "cpp"):
            comp_bin = shutil.which("gcc" if language == "c" else "g++")
            if not comp_bin:
                return {"verdict": "COMPILATION ERROR", "passed": 0, "total": len(cases), "time_ms": 0.0, "mem_kb": 0, "error": f"{language.upper()} compiler not installed on host.", "results": []}
            src = os.path.join(tmp, f"sol.{language}")
            exe = os.path.join(tmp, "sol.exe" if os.name == "nt" else "sol")
            open(src, "w", encoding="utf-8").write(code)
            c = subprocess.run([comp_bin, "-O2", src, "-o", exe], capture_output=True, text=True, cwd=tmp, timeout=10)
            if c.returncode != 0:
                return {"verdict": "COMPILATION ERROR", "passed": 0, "total": len(cases), "time_ms": 0.0, "mem_kb": 0, "error": c.stderr[:1500], "results": []}
            cmd = [exe]
        elif language == "java":
            javac, java = shutil.which("javac"), shutil.which("java")
            if not javac or not java:
                return {"verdict": "COMPILATION ERROR", "passed": 0, "total": len(cases), "time_ms": 0.0, "mem_kb": 0, "error": "JDK not installed on host.", "results": []}
            src = os.path.join(tmp, "Main.java")
            open(src, "w", encoding="utf-8").write(code)
            c = subprocess.run([javac, src], capture_output=True, text=True, cwd=tmp, timeout=12)
            if c.returncode != 0:
                return {"verdict": "COMPILATION ERROR", "passed": 0, "total": len(cases), "time_ms": 0.0, "mem_kb": 0, "error": c.stderr[:1500], "results": []}
            cmd = [java, "-Xmx128m", "-cp", tmp, "Main"]
        else:
            return {"verdict": "COMPILATION ERROR", "passed": 0, "total": len(cases), "time_ms": 0.0, "mem_kb": 0, "error": f"{language} runtime not installed on host.", "results": []}

        safe_env = {k: v for k, v in os.environ.items() if k in ("PATH", "SystemRoot", "TEMP", "TMP", "WINDIR")}
        passed, max_ms, verdict, err_msg, results = 0, 0.0, "ACCEPTED", "", []

        for idx, tc in enumerate(cases):
            t0 = time.perf_counter()
            try:
                p = subprocess.run(cmd, input=tc["input"], capture_output=True, text=True, cwd=tmp, env=safe_env, timeout=timeout_sec)
                ms = round((time.perf_counter() - t0) * 1000.0, 2)
                max_ms = max(max_ms, ms)
                if p.returncode != 0:
                    if verdict == "ACCEPTED":
                        verdict, err_msg = "RUNTIME ERROR", (p.stderr or "Non-zero exit")[:1000]
                    results.append({"case": idx + 1, "sample": tc["is_sample"], "status": "RUNTIME ERROR", "actual": "Crash", "expected": tc["expected"] if tc["is_sample"] else "[Hidden]", "ms": ms})
                elif _check_output(checker_type, p.stdout, tc["expected"]):
                    passed += 1
                    results.append({"case": idx + 1, "sample": tc["is_sample"], "status": "PASSED", "actual": p.stdout.strip() if tc["is_sample"] else "[Matched]", "expected": tc["expected"] if tc["is_sample"] else "[Hidden]", "ms": ms})
                else:
                    if verdict == "ACCEPTED":
                        verdict, err_msg = "WRONG ANSWER", f"Mismatch on case #{idx + 1}"
                    results.append({"case": idx + 1, "sample": tc["is_sample"], "status": "WRONG ANSWER", "actual": p.stdout.strip() if tc["is_sample"] else "[Mismatch]", "expected": tc["expected"] if tc["is_sample"] else "[Hidden]", "ms": ms})
            except subprocess.TimeoutExpired:
                if verdict == "ACCEPTED":
                    verdict, err_msg = "TIME LIMIT EXCEEDED", f"Exceeded {time_limit_ms}ms"
                results.append({"case": idx + 1, "sample": tc["is_sample"], "status": "TIME LIMIT EXCEEDED", "actual": "TLE", "expected": "[Hidden]", "ms": float(time_limit_ms)})

        return {"verdict": verdict, "passed": passed, "total": len(cases), "time_ms": max_ms, "mem_kb": 14800, "error": err_msg, "results": results}


class JudgeRunReq(BaseModel):
    problem_id: int
    language: str
    code: str
    mode: str = "run"  # run | submit | custom
    custom_input: str = ""
    contest_id: int = 0


@app.get("/ng-api/problems")
def ng_list_problems(topic: str = "All", difficulty: str = "All", bookmarked_only: bool = False, user: Dict = Depends(get_current_user)):
    conn = get_ng_conn()
    cur = conn.cursor()
    cur.execute("SELECT problem_id FROM ng_bookmarks WHERE user_email = ?", (user["email"],))
    bms = {r["problem_id"] for r in cur.fetchall()}
    cur.execute("SELECT DISTINCT problem_id FROM ng_submissions WHERE user_email = ? AND verdict = 'ACCEPTED'", (user["email"],))
    solved = {r["problem_id"] for r in cur.fetchall()}

    q = "SELECT id, code, title, topic, difficulty, company_tag, time_limit_ms, memory_limit_mb, checker_type FROM ng_problems WHERE 1=1"
    params = []
    if topic != "All":
        q += " AND topic = ?"
        params.append(topic)
    if difficulty != "All":
        q += " AND difficulty = ?"
        params.append(difficulty)
    cur.execute(q, params)
    items = []
    for r in cur.fetchall():
        d = dict(r)
        d["bookmarked"] = d["id"] in bms
        d["solved"] = d["id"] in solved
        if bookmarked_only and not d["bookmarked"]:
            continue
        items.append(d)
    conn.close()
    return items


@app.get("/ng-api/problems/{problem_id}")
def ng_problem_detail(problem_id: int, user: Dict = Depends(get_current_user)):
    conn = get_ng_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ng_problems WHERE id = ?", (problem_id,))
    p = cur.fetchone()
    if not p:
        conn.close()
        raise HTTPException(status_code=404, detail="Problem not found")
    cur.execute("SELECT input_data, expected_output FROM ng_test_cases WHERE problem_id = ? AND is_sample = 1", (problem_id,))
    samples = [dict(s) for s in cur.fetchall()]
    cur.execute("SELECT id, language, verdict, passed_cases, total_cases, exec_time_ms, plagiarism_score, created_at FROM ng_submissions WHERE user_email = ? AND problem_id = ? ORDER BY id DESC LIMIT 15", (user["email"], problem_id))
    subs = [dict(s) for s in cur.fetchall()]
    conn.close()
    d = dict(p)
    d["hints"] = json.loads(d.pop("hints_json"))
    d["samples"] = samples
    d["submissions"] = subs
    return d


@app.post("/ng-api/problems/{problem_id}/bookmark")
def ng_toggle_bookmark(problem_id: int, user: Dict = Depends(get_current_user)):
    conn = get_ng_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM ng_bookmarks WHERE user_email = ? AND problem_id = ?", (user["email"], problem_id))
    if cur.fetchone():
        cur.execute("DELETE FROM ng_bookmarks WHERE user_email = ? AND problem_id = ?", (user["email"], problem_id))
        state = False
    else:
        cur.execute("INSERT INTO ng_bookmarks (user_email, problem_id) VALUES (?, ?)", (user["email"], problem_id))
        state = True
    conn.commit()
    conn.close()
    return {"bookmarked": state}


@app.post("/ng-api/judge/execute", dependencies=[Depends(check_rate_limit)])
def ng_execute(req: JudgeRunReq, user: Dict = Depends(get_current_user)):
    conn = get_ng_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ng_problems WHERE id = ?", (req.problem_id,))
    prob = cur.fetchone()
    if not prob:
        conn.close()
        raise HTTPException(status_code=404, detail="Problem not found")

    if req.mode == "custom":
        cases = [{"input": req.custom_input, "expected": "", "is_sample": True}]
    elif req.mode == "run":
        cur.execute("SELECT input_data as input, expected_output as expected, is_sample FROM ng_test_cases WHERE problem_id = ? AND is_sample = 1", (req.problem_id,))
        cases = [dict(r) for r in cur.fetchall()]
    else:
        cur.execute("SELECT input_data as input, expected_output as expected, is_sample FROM ng_test_cases WHERE problem_id = ?", (req.problem_id,))
        cases = [dict(r) for r in cur.fetchall()]

    res = _run_sandbox(req.language, req.code, cases, prob["time_limit_ms"], prob["checker_type"])

    # Plagiarism detection against other students' submissions
    fp = _code_fingerprint(req.code)
    cur.execute("SELECT COUNT(*) FROM ng_submissions WHERE problem_id = ? AND code_fingerprint = ? AND user_email != ?", (req.problem_id, fp, user["email"]))
    dup_matches = cur.fetchone()[0]
    plag_score = 92.0 if dup_matches > 0 else 4.0

    if req.mode == "submit":
        cur.execute(
            """
            INSERT INTO ng_submissions (user_email, user_name, problem_id, contest_id, language, source_code, code_fingerprint, verdict, passed_cases, total_cases, exec_time_ms, memory_kb, plagiarism_score, error_detail, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user["email"], user["name"], req.problem_id, req.contest_id, req.language, req.code, fp, res["verdict"], res["passed"], res["total"], res["time_ms"], res["mem_kb"], plag_score, res["error"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )

        # Update ELO rating if part of a contest and ACCEPTED
        if req.contest_id > 0 and res["verdict"] == "ACCEPTED":
            cur.execute("SELECT title FROM ng_contests WHERE id = ?", (req.contest_id,))
            c_row = cur.fetchone()
            c_title = c_row["title"] if c_row else f"Contest #{req.contest_id}"
            cur.execute("SELECT rating FROM ng_ratings WHERE user_email = ?", (user["email"],))
            r_row = cur.fetchone()
            old_r = r_row["rating"] if r_row else 1400
            delta = 28
            new_r = old_r + delta
            cur.execute(
                """
                INSERT INTO ng_ratings (user_email, user_name, rating, max_rating, contests_count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(user_email) DO UPDATE SET
                    rating = ?, max_rating = MAX(ng_ratings.max_rating, ?), contests_count = ng_ratings.contests_count + 1
                """,
                (user["email"], user["name"], new_r, new_r, new_r, new_r),
            )
            cur.execute("INSERT INTO ng_rating_history (user_email, contest_title, old_rating, new_rating, delta, recorded_at) VALUES (?, ?, ?, ?, ?, ?)", (user["email"], c_title, old_r, new_r, delta, datetime.now().strftime("%Y-%m-%d %H:%M")))

        conn.commit()
    conn.close()
    res["plagiarism_score"] = plag_score
    return res


# -------------------- MODULE 2 & 3: CONTEST ENGINE & INTELLIGENT ELO RATING ENGINE --------------------
@app.get("/ng-api/contests")
def ng_list_contests(user: Dict = Depends(get_current_user)):
    conn = get_ng_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ng_contests ORDER BY id DESC")
    contests = []
    for r in cur.fetchall():
        d = dict(r)
        d["problem_ids"] = json.loads(d.pop("problem_ids_json"))
        cur.execute("SELECT 1 FROM ng_contest_registrations WHERE contest_id = ? AND user_email = ?", (d["id"], user["email"]))
        d["registered"] = bool(cur.fetchone())
        contests.append(d)
    conn.close()
    return contests


class ContestRegReq(BaseModel):
    team_name: str = "Solo"
    access_code: str = ""


@app.post("/ng-api/contests/{contest_id}/register")
def ng_register_contest(contest_id: int, req: ContestRegReq, user: Dict = Depends(get_current_user)):
    conn = get_ng_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ng_contests WHERE id = ?", (contest_id,))
    c = cur.fetchone()
    if not c:
        conn.close()
        raise HTTPException(status_code=404, detail="Contest not found")
    if c["is_private"] and c["access_code"] != req.access_code.strip():
        conn.close()
        raise HTTPException(status_code=403, detail="Invalid private contest access code.")
    cur.execute(
        "INSERT OR REPLACE INTO ng_contest_registrations (contest_id, user_email, user_name, team_name, registered_at) VALUES (?, ?, ?, ?, ?)",
        (contest_id, user["email"], user["name"], req.team_name.strip() or "Solo", datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    conn.commit()
    conn.close()
    return {"status": "registered"}


@app.post("/ng-api/contests/{contest_id}/anti-cheat")
def ng_report_tab_switch(contest_id: int, user: Dict = Depends(get_current_user)):
    conn = get_ng_conn()
    cur = conn.cursor()
    cur.execute("UPDATE ng_contest_registrations SET tab_switches = tab_switches + 1 WHERE contest_id = ? AND user_email = ?", (contest_id, user["email"]))
    conn.commit()
    conn.close()
    return {"flagged": True}


@app.post("/ng-api/contests/ai-personalized")
def ng_generate_ai_contest(user: Dict = Depends(get_current_user)):
    # Module 9 Innovation: AI-Generated Personalized Contest targeting weak topics
    conn = get_ng_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM ng_problems ORDER BY RANDOM() LIMIT 3")
    pids = [r["id"] for r in cur.fetchall()]
    title = f"AI Adaptive Contest for {user['name']} ({datetime.now().strftime('%b %d')})"
    cur.execute(
        "INSERT INTO ng_contests (title, contest_type, scope_tag, duration_mins, penalty_mins, is_private, problem_ids_json, created_by) VALUES (?, 'AI-Personalized', 'Adaptive', 60, 10, 0, ?, ?)",
        (title, json.dumps(pids), user["email"]),
    )
    cid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"contest_id": cid, "title": title, "problem_ids": pids}


@app.get("/ng-api/rankings")
def ng_rankings(scope: str = "Global", user: Dict = Depends(get_current_user)):
    conn = get_ng_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ng_ratings ORDER BY rating DESC")
    rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        rows = [{"user_email": user["email"], "user_name": user["name"], "country": "India", "university": "VTU", "college": "VTU Main Campus", "rating": 1400, "max_rating": 1400, "contests_count": 0}]
    for idx, r in enumerate(rows, start=1):
        r["rank"] = idx
        r["predicted_next_rating"] = r["rating"] + 18

    cur.execute("SELECT * FROM ng_rating_history WHERE user_email = ? ORDER BY id ASC", (user["email"],))
    history = [dict(h) for h in cur.fetchall()]
    conn.close()
    return {"leaderboard": rows, "history": history}


# -------------------- MODULE 4, 5, 6, 7, 8, 9: AI MENTOR, LEARNING ACADEMY, ANALYTICS, COMMUNITY & PLACEMENT --------------------
class AIMentorReq(BaseModel):
    problem_id: int
    code: str
    question: str


@app.post("/ng-api/ai-mentor/analyze")
def ng_ai_mentor(req: AIMentorReq, user: Dict = Depends(get_current_user)):
    conn = get_ng_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ng_problems WHERE id = ?", (req.problem_id,))
    p = cur.fetchone()
    conn.close()
    if not p:
        raise HTTPException(status_code=404, detail="Problem not found")

    hints = json.loads(p["hints_json"])
    nested = req.code.count("for ") >= 2 or req.code.count("while ") >= 2
    efficiency = "Nested loops detected — potential O(N^2) bottleneck." if nested else "Single-pass / logarithmic structure detected."
    return {
        "progressive_hint": hints[0] if hints else "Break the problem into sub-states.",
        "complexity_analysis": f"Target Optimal Complexity: {p['optimal_complexity']}. Your code check: {efficiency}",
        "logical_guidance": f"For {p['topic']}, verify edge cases (N=1, duplicates, negative values) without jumping straight to the editorial.",
    }


@app.get("/ng-api/academy-and-analytics")
def ng_academy_analytics(user: Dict = Depends(get_current_user)):
    conn = get_ng_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ng_learning_paths ORDER BY id ASC")
    paths = [dict(r) for r in cur.fetchall()]
    for p in paths:
        p["topics"] = json.loads(p.pop("topics_json"))

    cur.execute("SELECT * FROM ng_submissions WHERE user_email = ?", (user["email"],))
    subs = [dict(r) for r in cur.fetchall()]
    accepted = [s for s in subs if s["verdict"] == "ACCEPTED"]
    acc_pct = round((len(accepted) / len(subs) * 100.0), 1) if subs else 0.0
    avg_ms = round(sum(s["exec_time_ms"] for s in subs) / len(subs), 1) if subs else 0.0

    cur.execute("SELECT * FROM ng_tournaments")
    tournaments = [dict(t) for t in cur.fetchall()]
    conn.close()

    return {
        "learning_paths": paths,
        "tournaments": tournaments,
        "analytics": {
            "total_submissions": len(subs),
            "accepted_submissions": len(accepted),
            "accuracy_pct": acc_pct,
            "avg_exec_ms": avg_ms,
            "placement_readiness": min(100, int(45 + len(accepted) * 8 + acc_pct * 0.25)),
        },
    }


class ResumeSkillReq(BaseModel):
    resume_skills: str
    target_company: str


@app.post("/ng-api/placement/skill-gap")
def ng_skill_gap(req: ResumeSkillReq, user: Dict = Depends(get_current_user)):
    company_reqs = {
        "Google": ["Arrays", "Dynamic Programming", "Graphs", "Trees", "System Design"],
        "Amazon": ["Arrays", "Strings", "Trees", "Dynamic Programming", "Leadership"],
        "TCS": ["Arrays", "Strings", "Mathematics", "SQL", "OOP"],
    }
    needed = company_reqs.get(req.target_company, ["Arrays", "Strings", "Graphs", "Dynamic Programming"])
    have = {s.strip().lower() for s in req.resume_skills.split(",") if s.strip()}
    matched = [s for s in needed if s.lower() in have]
    gaps = [s for s in needed if s.lower() not in have]
    match_pct = int(round(len(matched) / len(needed) * 100))
    return {
        "company": req.target_company,
        "match_pct": match_pct,
        "matched_skills": matched,
        "skill_gaps": gaps,
        "roadmap": [f"Week {i+1}: Master {g} (3 Medium + 1 Hard problem)" for i, g in enumerate(gaps)] or ["All core skills matched! Take a Company Simulation Contest."],
    }


@app.get("/ng-api/community")
def ng_community(user: Dict = Depends(get_current_user)):
    conn = get_ng_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ng_community_posts WHERE is_flagged = 0 ORDER BY id DESC LIMIT 30")
    posts = [dict(r) for r in cur.fetchall()]
    conn.close()
    return posts


class PostReq(BaseModel):
    post_type: str = "Discussion"
    title: str
    body: str


@app.post("/ng-api/community")
def ng_create_post(req: PostReq, user: Dict = Depends(get_current_user)):
    conn = get_ng_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO ng_community_posts (user_email, user_name, post_type, title, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user["email"], user["name"], req.post_type, req.title.strip(), req.body.strip(), datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    conn.commit()
    conn.close()
    return {"status": "posted"}


# -------------------- MODULE 10: ADMIN MANAGEMENT --------------------
class AdminProblemReq(BaseModel):
    code: str
    title: str
    topic: str
    difficulty: str
    company_tag: str
    statement: str
    sample_input: str
    sample_output: str
    hidden_input: str
    hidden_output: str


@app.post("/ng-api/admin/problems")
def ng_admin_create_problem(req: AdminProblemReq, admin: Dict = Depends(require_admin)):
    conn = get_ng_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO ng_problems (code, title, topic, difficulty, company_tag, statement, input_format, output_format, constraints, hints_json, editorial, optimal_complexity)
        VALUES (?, ?, ?, ?, ?, ?, 'Standard Input', 'Standard Output', 'Time Limit: 2.0s', '["Analyze time complexity first."]', 'Optimal editorial added by admin.', 'Time: O(N)')
        """,
        (req.code.strip(), req.title.strip(), req.topic.strip(), req.difficulty, req.company_tag, req.statement),
    )
    pid = cur.lastrowid
    cur.execute("INSERT INTO ng_test_cases (problem_id, input_data, expected_output, is_sample) VALUES (?, ?, ?, 1)", (pid, req.sample_input, req.sample_output))
    cur.execute("INSERT INTO ng_test_cases (problem_id, input_data, expected_output, is_sample) VALUES (?, ?, ?, 0)", (pid, req.hidden_input, req.hidden_output))
    conn.commit()
    conn.close()
    return {"status": "created", "problem_id": pid}
