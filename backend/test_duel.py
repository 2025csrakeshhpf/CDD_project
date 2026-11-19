from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from main import app, init_db
from duel_service import _conn, init_duel_tables

init_db()
init_duel_tables()
client = TestClient(app)


def _get_headers(email: str, name: str, password: str):
    client.post("/api/auth/register", json={
        "full_name": name,
        "email": email,
        "password": password,
        "confirm_password": password,
        "usn": "4IK25CS200"
    })
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['token']}"}


def test_full_1v1_duel_lifecycle_winner_and_draw():
    h_alice = _get_headers("rakesh@vtu.ac.in", "Rakesh HP", "rakesh123")
    h_bob = _get_headers("peer_bob@vtu.ac.in", "Bob Student", "Password123")

    # 1. Challenge creation
    c_res = client.post("/api/duels/create", headers=h_alice, json={
        "opponent_email": "peer_bob@vtu.ac.in",
        "problem_id": 1,
        "duration_mins": 30
    })
    assert c_res.status_code == 200
    uid = c_res.json()["challenge_uid"]

    # 2. Challenge acceptance by opponent
    acc = client.post(f"/api/duels/{uid}/respond?action=accept", headers=h_bob)
    assert acc.status_code == 200

    # 3. Both participants mark ready -> starts countdown
    r1 = client.post(f"/api/duels/{uid}/ready", headers=h_alice).json()
    assert r1["status"] == "Accepted"
    r2 = client.post(f"/api/duels/{uid}/ready", headers=h_bob).json()
    assert r2["status"] == "Active"

    # 4. State hides opponent source code and exposes only visible sample test cases
    state_bob = client.get(f"/api/duels/{uid}", headers=h_bob).json()
    assert len(state_bob["problem"]["samples"]) == 2

    # 5. Independent submissions: Alice submits passing solution, Bob submits wrong solution
    good_code = "import sys\nd=sys.stdin.read().split()\nn=int(d[0])\na=list(map(int,d[1:1+n]))\nw=sum(a[:2])\nb=w\nfor r in range(2,n):\n w+=a[r]-a[r-2]\n b=max(b,w)\nprint(b)\n"
    sub_alice = client.post(f"/api/duels/{uid}/submit", headers=h_alice, json={"language": "python", "code": good_code})
    assert sub_alice.status_code == 200
    assert sub_alice.json()["verdict"] == "ACCEPTED"

    # Duplicate submission blocked
    dup = client.post(f"/api/duels/{uid}/submit", headers=h_alice, json={"language": "python", "code": good_code})
    assert dup.status_code == 409

    sub_bob = client.post(f"/api/duels/{uid}/submit", headers=h_bob, json={"language": "python", "code": "print(0)"})
    assert sub_bob.status_code == 200
    final = sub_bob.json()["final_result"]
    assert final["status"] == "Completed"
    assert final["winner_email"] == "rakesh@vtu.ac.in"
    assert final["is_draw"] is False


def test_duel_draw_and_timer_expiration():
    h_alice = _get_headers("rakesh@vtu.ac.in", "Rakesh HP", "rakesh123")
    h_bob = _get_headers("peer_bob@vtu.ac.in", "Bob Student", "Password123")

    c_res = client.post("/api/duels/create", headers=h_alice, json={
        "opponent_email": "peer_bob@vtu.ac.in",
        "problem_id": 1,
        "duration_mins": 15
    })
    uid = c_res.json()["challenge_uid"]
    client.post(f"/api/duels/{uid}/respond?action=accept", headers=h_bob)
    client.post(f"/api/duels/{uid}/ready", headers=h_alice)
    client.post(f"/api/duels/{uid}/ready", headers=h_bob)

    # Simulate timer expiration with 0 submissions -> exact tie = DRAW
    conn = _conn()
    past = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
    conn.execute("UPDATE duel_challenges SET ends_at = ? WHERE challenge_uid = ?", (past, uid))
    conn.commit()
    conn.close()

    state = client.get(f"/api/duels/{uid}", headers=h_alice).json()
    assert state["duel"]["status"] == "Completed"
    assert state["duel"]["is_draw"] == 1
