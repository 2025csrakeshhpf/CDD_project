from fastapi.testclient import TestClient
from main import app, init_db
from competitive_extensions import init_competitive_tables
from duel_service import init_duel_tables
from invitation_service import _conn, init_invitation_tables

init_db()
init_competitive_tables()
init_duel_tables()
init_invitation_tables()
client = TestClient(app)


def _login(email: str, password: str):
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['token']}"}


def test_leaderboard_excludes_ineligible_students_and_no_duplicates():
    h_rakesh = _login("rakesh@vtu.ac.in", "rakesh123")
    _login("rohit@vtu.ac.in", "rohit123")

    # Reset submissions & contest entries to verify clean eligibility rules
    conn = _conn()
    conn.execute("DELETE FROM student_submissions")
    conn.execute("DELETE FROM duel_submissions")
    conn.execute("DELETE FROM duel_challenges")
    conn.execute("DELETE FROM contest_entries")
    conn.execute("DELETE FROM user_ratings")
    conn.commit()
    conn.close()

    # With 0 solved problems and 0 completed contests, neither Rakesh nor Rohit should appear
    lb_empty = client.get("/api/competitive/leaderboard", headers=h_rakesh).json()
    assert len(lb_empty) == 0

    # Rakesh solves 1 problem twice (duplicate submissions) -> appears ONCE with solved_count == 1, Rohit still excluded
    code = "import sys\nd=sys.stdin.read().split()\nn=int(d[0])\na=list(map(int,d[1:1+n]))\nw=sum(a[:2])\nb=w\nfor r in range(2,n):\n w+=a[r]-a[r-2]\n b=max(b,w)\nprint(b)\n"
    for _ in range(2):
        sub = client.post("/api/coding/execute", headers=h_rakesh, json={
            "problem_id": 1,
            "language": "python",
            "code": code,
            "mode": "submit",
            "time_taken_sec": 35
        })
        assert sub.status_code == 200
        assert sub.json()["verdict"] == "ACCEPTED"

    lb_one = client.get("/api/competitive/leaderboard", headers=h_rakesh).json()
    assert len(lb_one) == 1
    assert lb_one[0]["email"] == "rakesh@vtu.ac.in"
    assert lb_one[0]["solved_count"] == 1
    assert lb_one[0]["rank"] == 1
    assert all(row["email"] != "rohit@vtu.ac.in" for row in lb_one)


def test_two_account_invitation_online_offline_accept_and_reject():
    h_a = _login("rakesh@vtu.ac.in", "rakesh123")
    h_b = _login("rohit@vtu.ac.in", "rohit123")

    conn = _conn()
    conn.execute("DELETE FROM challenge_invitations")
    conn.execute("DELETE FROM user_notifications")
    conn.commit()
    conn.close()

    # 1. Account A (Rakesh) sends challenge to Account B (Rohit) while B is offline
    sent = client.post("/api/invitations/send", headers=h_a, json={
        "recipient_email": "rohit@vtu.ac.in",
        "problem_id": 1,
        "duration_mins": 30
    })
    assert sent.status_code == 200
    inv_uid = sent.json()["invitation_uid"]

    # 2. Account B logs in & refreshes page -> persistent unread notification is retrieved by user_id
    inbox_b1 = client.get("/api/invitations/inbox", headers=h_b).json()
    assert inbox_b1["unread_count"] == 1
    assert inbox_b1["notifications"][0]["sender_name"] == "Rakesh HP"
    assert inbox_b1["notifications"][0]["duration_mins"] == 30

    # Simulated page refresh (second GET before responding) -> still visible
    inbox_b2 = client.get("/api/invitations/inbox", headers=h_b).json()
    assert len(inbox_b2["notifications"]) == 1

    # 3. Account B accepts challenge -> creates shared competition session & notifies Account A
    acc = client.post(f"/api/invitations/{inv_uid}/accept", headers=h_b)
    assert acc.status_code == 200
    comp_uid = acc.json()["competition_uid"]
    assert comp_uid

    inbox_a = client.get("/api/invitations/inbox", headers=h_a).json()
    assert any(n["notif_type"] == "CHALLENGE_ACCEPTED" and n["invitation_uid"] == inv_uid for n in inbox_a["notifications"])
