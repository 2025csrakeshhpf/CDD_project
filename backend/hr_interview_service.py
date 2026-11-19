import os
import re
import json
import sqlite3
import urllib.request
from datetime import datetime
from typing import Dict, Any, List, Optional

DEFAULT_RUBRIC = {
    "relevance": 25,
    "communication": 20,
    "clarity": 15,
    "problem_solving": 15,
    "professional_attitude": 10,
    "completeness": 15,
}


def get_passing_score() -> int:
    try:
        return int(os.getenv("HR_PASSING_SCORE", "60"))
    except ValueError:
        return 60


def init_hr_interview_tables(db_path: str = "placement.db"):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hr_interview_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            student_name TEXT NOT NULL,
            student_email TEXT,
            interview_date TEXT NOT NULL,
            total_questions INTEGER NOT NULL,
            satisfactory_count INTEGER NOT NULL,
            insufficient_count INTEGER NOT NULL,
            total_score REAL NOT NULL,
            percentage REAL NOT NULL,
            status TEXT NOT NULL,
            passing_threshold INTEGER NOT NULL,
            rubric_breakdown_json TEXT,
            report_json TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            student_name TEXT,
            student_email TEXT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            page_name TEXT,
            dedup_hash TEXT UNIQUE,
            email_sent INTEGER DEFAULT 0,
            email_error TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def _classify_question(question: str) -> str:
    q = question.lower()
    if any(w in q for w in ("tell me about a time", "conflict", "mistake", "failure", "team", "pressure", "weakness", "challenge", "leadership")):
        return "behavioral"
    if any(w in q for w in ("complex", "algorithm", "database", "system", "api", "debug", "project", "architecture", "sql", "code", "oop")):
        return "technical"
    return "general_hr"


def _semantic_heuristic_evaluate(question: str, answer: str, q_type: str) -> Dict[str, Any]:
    """
    Evaluates relevance, communication, clarity, reasoning, attitude, and completeness
    without keyword-matching a single canned script and WITHOUT revealing answers.
    """
    clean = (answer or "").strip()
    words = re.findall(r"\b[a-zA-Z0-9']+\b", clean.lower())
    word_count = len(words)
    sentences = [s.strip() for s in re.split(r"[.!?]+", clean) if len(s.strip()) > 2]

    # Stopwords removed to test topical overlap / semantic relevance to question
    stop = {"the", "is", "at", "which", "on", "and", "a", "an", "to", "in", "for", "of", "with", "as", "by", "this", "that", "it", "you", "your", "me", "my", "about", "what", "how", "why", "tell"}
    q_terms = {w for w in re.findall(r"\b[a-zA-Z]{3,}\b", question.lower()) if w not in stop}
    a_terms = {w for w in words if len(w) >= 3 and w not in stop}

    # Dismissive / non-answers
    evasive_phrases = ("i don't know", "idk", "no idea", "skip", "next question", "pass", "nothing", "n/a", "none")
    if word_count < 6 or any(clean.lower() == p or clean.lower().startswith(p + ".") for p in evasive_phrases):
        return {
            "is_satisfactory": False,
            "feedback": "Your answer is too brief and does not address the question. Please provide a complete, structured response in the next question.",
            "missing_aspect": "Sufficient detail, reasoning, and direct engagement with the prompt",
            "scores": {"relevance": 4, "communication": 5, "clarity": 4, "problem_solving": 3, "professional_attitude": 5, "completeness": 3},
        }

    # Reasoning & STAR structure signals (works across varied phrasings)
    reasoning_markers = {"because", "since", "therefore", "so", "result", "learned", "improved", "approach", "ensured", "resolved", "implemented", "analyzed", "collaborated", "impact", "outcome", "step", "first", "finally"}
    professional_markers = {"team", "learn", "responsibility", "growth", "improve", "quality", "deadline", "goal", "feedback", "adapt", "solution", "user", "client", "project"}
    unprofessional_markers = {"hate", "stupid", "lazy", "fault", "blame", "whatever", "don't care", "boring"}

    has_reasoning = len(reasoning_markers.intersection(words)) >= (2 if q_type in ("behavioral", "technical") else 1)
    has_unprofessional = len(unprofessional_markers.intersection(words)) > 0
    topical_connection = len(q_terms.intersection(a_terms)) >= 1 or word_count >= 18

    # Score calculation (out of 100)
    relevance = 22 if topical_connection and word_count >= 15 else (13 if topical_connection else 7)
    communication = 18 if len(sentences) >= 2 and 18 <= word_count <= 220 else (12 if word_count >= 10 else 7)
    clarity = 14 if len(sentences) >= 2 and not clean.isupper() else 9
    problem_solving = 14 if has_reasoning else (8 if word_count >= 16 else 5)
    professional_attitude = 3 if has_unprofessional else (9 if len(professional_markers.intersection(words)) >= 1 or word_count >= 15 else 7)
    completeness = 14 if word_count >= 24 and has_reasoning else (9 if word_count >= 15 else 5)

    total = relevance + communication + clarity + problem_solving + professional_attitude + completeness
    is_satisfactory = (total >= 62) and topical_connection and (not has_unprofessional) and (word_count >= 14)

    if is_satisfactory:
        feedback = "Good answer. You explained your reasoning clearly and stayed relevant to the question. Let's move to the next question."
        missing = None
    else:
        if has_unprofessional:
            missing = "Professional tone and constructive framing"
            feedback = "Your response needs a more professional and constructive tone. Let's move on to the next question."
        elif not topical_connection:
            missing = "Direct relevance to what the question actually asked"
            feedback = "Your answer is not sufficiently relevant to the question asked. Try to address the core prompt directly in future answers."
        elif not has_reasoning:
            missing = "Clear reasoning, specific action steps, or measurable outcome"
            feedback = "Your answer is incomplete. You need to explain your reasoning and specific actions more clearly. Let's move to the next question."
        else:
            missing = "Greater depth and concrete supporting details"
            feedback = "Your answer needs more depth and completeness to fully address the question. Let's move on."

    return {
        "is_satisfactory": is_satisfactory,
        "feedback": feedback,
        "missing_aspect": missing,
        "scores": {
            "relevance": relevance,
            "communication": communication,
            "clarity": clarity,
            "problem_solving": problem_solving,
            "professional_attitude": professional_attitude,
            "completeness": completeness,
        },
    }


def evaluate_interview_answer(
    question: str,
    answer: str,
    question_index: int = 1,
    total_questions: int = 5,
    rubric: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """
    Evaluates a single HR interview turn.
    Strictly avoids leaking correct/model answers while giving realistic HR feedback.
    """
    active_rubric = rubric or DEFAULT_RUBRIC
    q_type = _classify_question(question)
    api_key = os.getenv("LLM_API_KEY", "").strip()

    result = None
    if api_key:
        try:
            prompt = (
                "You are a realistic corporate HR interviewer conducting a student placement mock interview. "
                "Evaluate the candidate's answer fairly based on meaning, relevance, clarity, reasoning, attitude, and completeness "
                "(accept valid paraphrasing; do NOT require exact keyword matches). "
                "CRITICAL RULE: If the answer is weak or wrong, explain briefly what aspect is missing, "
                "but NEVER reveal the correct answer, model answer, or solution hints. Keep feedback to 1-2 short professional sentences.\n"
                f"Question Type: {q_type}\nQuestion: {question}\nCandidate Answer: {answer}\n"
                'Return ONLY valid JSON: {"is_satisfactory": bool, "feedback": str, "missing_aspect": str|null, '
                '"scores": {"relevance": 0-25, "communication": 0-20, "clarity": 0-15, "problem_solving": 0-15, "professional_attitude": 0-10, "completeness": 0-15}}'
            )
            payload = json.dumps({
                "model": os.getenv("LLM_MODEL", "gpt-4o-mini"),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            }).encode("utf-8")
            req = urllib.request.Request(
                os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions"),
                data=payload,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                content = body["choices"][0]["message"]["content"].strip()
                content = re.sub(r"^```json|```$", "", content).strip()
                result = json.loads(content)
        except Exception:
            result = None

    if not result:
        result = _semantic_heuristic_evaluate(question, answer, q_type)

    scores = result["scores"]
    turn_score = sum(min(float(scores.get(k, 0)), active_rubric[k]) for k in active_rubric)
    is_last = question_index >= total_questions

    return {
        "question_index": question_index,
        "question_type": q_type,
        "is_satisfactory": bool(result["is_satisfactory"]),
        "feedback": result["feedback"],
        "missing_aspect": result.get("missing_aspect"),
        "turn_score": round(turn_score, 1),
        "rubric_scores": scores,
        "proceed_to_next": not is_last,
        "interview_completed": is_last,
    }


def finalize_hr_interview(
    student_name: str,
    turns: List[Dict[str, Any]],
    user_id: Optional[int] = None,
    student_email: Optional[str] = None,
    db_path: str = "placement.db",
) -> Dict[str, Any]:
    """
    Aggregates per-question evaluations into the final 100-mark placement preparation report,
    applies the configurable passing threshold, and saves to hr_interview_sessions.
    """
    init_hr_interview_tables(db_path)
    threshold = get_passing_score()
    total_q = max(len(turns), 1)
    satisfactory_count = sum(1 for t in turns if t.get("is_satisfactory"))
    insufficient_count = len(turns) - satisfactory_count

    # Average each rubric category across all questions
    avg_rubric = {k: 0.0 for k in DEFAULT_RUBRIC}
    for t in turns:
        rs = t.get("rubric_scores", {})
        for k in DEFAULT_RUBRIC:
            avg_rubric[k] += float(rs.get(k, 0))
    for k in avg_rubric:
        avg_rubric[k] = round(avg_rubric[k] / total_q, 1)

    total_score = round(sum(avg_rubric.values()), 1)
    percentage = round((total_score / 100.0) * 100.0, 1)
    status = "PASS" if total_score >= threshold else "FAIL"
    interview_date = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Strengths & Areas for improvement derived directly from rubric scores
    strengths = []
    improvements = []
    label_map = {
        "relevance": "Directly addressing interview questions with relevant context",
        "communication": "Clear and structured verbal/written communication",
        "clarity": "Concise expression and confident delivery",
        "problem_solving": "Logical reasoning and structured STAR problem-solving",
        "professional_attitude": "Positive, growth-oriented professional demeanor",
        "completeness": "Providing thorough examples with concrete outcomes",
    }
    for k, max_pts in DEFAULT_RUBRIC.items():
        ratio = avg_rubric[k] / max_pts if max_pts else 0
        if ratio >= 0.72:
            strengths.append(label_map[k])
        elif ratio < 0.60:
            improvements.append(label_map[k])

    if not strengths:
        strengths.append("Completed all interview questions and engaged with the prompt")
    if not improvements:
        improvements.append("Continue practicing timed behavioral examples with quantifiable results")

    comm_assessment = (
        "Strong, articulate communication suitable for campus placement rounds."
        if avg_rubric["communication"] >= 15
        else "Needs clearer sentence structure, pacing, and elaboration when explaining ideas."
    )
    quality_assessment = (
        f"{satisfactory_count} of {len(turns)} answers met placement readiness standards."
        if satisfactory_count > insufficient_count
        else f"Only {satisfactory_count} of {len(turns)} answers were sufficiently complete and relevant."
    )
    summary = (
        f"Placement Preparation Assessment ({status}): You scored {total_score}/100 ({percentage}%), "
        f"against a passing benchmark of {threshold}/100. This is a practice evaluation to guide your preparation, not an official hiring decision."
    )
    suggestions = [
        "Use the STAR method (Situation, Task, Action, Result) for behavioral questions.",
        "Avoid one-line answers; explain the 'why' behind your technical and teamwork decisions.",
        "Keep answers focused directly on the competency asked by the interviewer.",
    ]

    report = {
        "student_name": student_name,
        "interview_date": interview_date,
        "total_questions": len(turns),
        "satisfactory_answers": satisfactory_count,
        "insufficient_or_incorrect_answers": insufficient_count,
        "total_score": total_score,
        "max_score": 100,
        "percentage": percentage,
        "status": status,
        "passing_threshold": threshold,
        "rubric_breakdown": avg_rubric,
        "communication_assessment": comm_assessment,
        "answer_quality_assessment": quality_assessment,
        "strengths": strengths,
        "areas_requiring_improvement": improvements,
        "interview_summary": summary,
        "suggestions_for_future_preparation": suggestions,
        "disclaimer": "Placement preparation assessment only; not an official hiring decision.",
    }

    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """
        INSERT INTO hr_interview_sessions
        (user_id, student_name, student_email, interview_date, total_questions,
         satisfactory_count, insufficient_count, total_score, percentage, status,
         passing_threshold, rubric_breakdown_json, report_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            student_name,
            student_email,
            interview_date,
            len(turns),
            satisfactory_count,
            insufficient_count,
            total_score,
            percentage,
            status,
            threshold,
            json.dumps(avg_rubric),
            json.dumps(report),
        ),
    )
    report["session_id"] = cur.lastrowid
    conn.commit()
    conn.close()
    return report


def get_hr_interview_history(user_id: Optional[int] = None, student_name: Optional[str] = None, db_path: str = "placement.db") -> List[Dict[str, Any]]:
    init_hr_interview_tables(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if user_id is not None:
        rows = conn.execute(
            "SELECT * FROM hr_interview_sessions WHERE user_id = ? ORDER BY id DESC", (user_id,)
        ).fetchall()
    elif student_name:
        rows = conn.execute(
            "SELECT * FROM hr_interview_sessions WHERE student_name = ? ORDER BY id DESC", (student_name,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM hr_interview_sessions ORDER BY id DESC LIMIT 25").fetchall()
    conn.close()

    history = []
    for r in rows:
        item = dict(r)
        if item.get("report_json"):
            item["report"] = json.loads(item["report_json"])
        history.append(item)
    return history
