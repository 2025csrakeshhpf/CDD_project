from unittest.mock import patch
from fastapi.testclient import TestClient
from main import app
from hr_interview_service import evaluate_interview_answer, finalize_hr_interview

client = TestClient(app)


def test_hr_interview_good_vs_bad_and_no_answer_leak():
    q = "Tell me about a time you resolved a bug or conflict in a team project."

    # 1. Good structured answer (paraphrased naturally)
    good = evaluate_interview_answer(
        question=q,
        answer=(
            "During our second-year web project, our API responses were slow because of unindexed queries. "
            "I analyzed the query execution plan, added composite indexes, and collaborated with my teammate to verify latency. "
            "As a result, page load time dropped by 60 percent before our final demo."
        ),
        question_index=1,
        total_questions=3,
    )
    assert good["is_satisfactory"] is True
    assert good["turn_score"] >= 65
    assert good["proceed_to_next"] is True

    # 2. Vague / insufficient answer -> warning without revealing model solution
    vague = evaluate_interview_answer(
        question=q,
        answer="We had an issue in code and I fixed it quickly.",
        question_index=2,
        total_questions=3,
    )
    assert vague["is_satisfactory"] is False
    assert "incomplete" in vague["feedback"].lower() or "relevant" in vague["feedback"].lower() or "depth" in vague["feedback"].lower()
    assert "star" not in vague["feedback"].lower()  # Does not leak solution template mid-interview

    # 3. Irrelevant / evasive answer
    irrelevant = evaluate_interview_answer(
        question=q,
        answer="I don't know",
        question_index=3,
        total_questions=3,
    )
    assert irrelevant["is_satisfactory"] is False
    assert irrelevant["interview_completed"] is True


def test_hr_interview_pass_and_fail_thresholds():
    good_turn = evaluate_interview_answer(
        "Why should we hire you for a software engineering role?",
        "I have strong problem-solving fundamentals in C++ and Python, built a full-stack placement tracker with WebSockets, "
        "and consistently take ownership of debugging and testing because I want to deliver reliable software for users.",
    )
    pass_report = finalize_hr_interview("Rakesh HP", [good_turn, good_turn], student_email="rakesh@vtu.ac.in")
    assert pass_report["status"] == "PASS"
    assert pass_report["total_score"] >= 60
    assert "official hiring decision" in pass_report["disclaimer"].lower()

    weak_turn = evaluate_interview_answer("Why should we hire you?", "No idea.")
    fail_report = finalize_hr_interview("Rakesh HP", [weak_turn, weak_turn], student_email="rakesh@vtu.ac.in")
    assert fail_report["status"] == "FAIL"
    assert fail_report["total_score"] < 60


def test_peer_challenge_email_success_and_failure_handling():
    payload = {
        "recipient_email": "rohit@vtu.ac.in",
        "recipient_name": "Rohit",
        "challenger_name": "Rakesh HP",
        "problem_title": "Two Sum",
        "difficulty": "Easy",
        "duration_mins": 30,
        "invitation_uid": "inv-test-123",
    }
    with patch("main.send_peer_challenge_email", return_value={"sent": True, "error": None}):
        ok = client.post("/api/invitations/notify-email", json=payload)
        assert ok.status_code == 200
        assert ok.json()["email_sent"] is True

    with patch("main.send_peer_challenge_email", return_value={"sent": False, "error": "SMTP auth failed"}):
        fail = client.post("/api/invitations/notify-email", json=payload)
        assert fail.status_code == 200
        assert fail.json()["email_sent"] is False
        assert "SMTP auth failed" in fail.json()["message"]


def test_bug_and_suggestion_feedback_email_and_db_safety():
    # Even if SMTP fails, feedback is preserved in SQLite and never falsely reports email_sent=True
    with patch("main.send_feedback_email", return_value={"sent": False, "error": "SMTP offline"}):
        res = client.post("/api/feedback/submit", json={
            "category": "Bug Report",
            "title": "Timer flicker on duel start",
            "description": "Countdown jumps by 1 second on initial WebSocket connect.",
            "page_name": "1v1 Coding Duel",
        })
        assert res.status_code == 200
        data = res.json()
        assert data["saved"] is True
        assert data["email_sent"] is False

    # Successful email delivery to rakeskhp2142@gmail.com
    with patch("main.send_feedback_email", return_value={"sent": True, "error": None}):
        res2 = client.post("/api/feedback/submit", json={
            "category": "Suggestion",
            "title": "Add dark mode toggle to leaderboard",
            "description": "A high-contrast theme for late-night practice sessions would help.",
            "page_name": "Leaderboard",
        })
        assert res2.status_code == 200
        assert res2.json()["saved"] is True
        assert res2.json()["email_sent"] is True
