from fastapi.testclient import TestClient
from main import app, init_db
from duel_service import init_duel_tables
from invitation_service import _conn, init_invitation_tables

init_db()
init_duel_tables()
init_invitation_tables()
client = TestClient(app)


def _login(email: str, password: str):
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['token']}"}


def test_rakesh_and_rohit_invitation_accept_reject_and_offline_delivery():
    h_rakesh = _login("rakesh@vtu.ac.in", "rakesh123")
    h_rohit = _login("rohit@vtu.ac.in", "rohit123")

    # Clean prior test records between Rakesh and Rohit
    conn = _conn()
    conn.execute("DELETE FROM challenge_invitations")
    conn.execute("DELETE FROM user_notifications")
    conn.commit()
    conn.close()

    # Prevent self-challenge
    self_res = client.post("/api/invitations/send", headers=h_rakesh, json={
        "recipient_email": "rakesh@vtu.ac.in",
        "problem_id": 1,
        "duration_mins": 30
    })
    assert self_res.status_code == 400

    # 1. Rakesh sends invitation to Rohit while Rohit is offline
    send_res = client.post("/api/invitations/send", headers=h_rakesh, json={
        "recipient_email": "rohit@vtu.ac.in",
        "problem_id": 1,
        "duration_mins": 30
    })
    assert send_res.status_code == 200
    inv_uid = send_res.json()["invitation_uid"]
    assert send_res.json()["status"] == "PENDING"

    # Prevent duplicate pending invitation
    dup_res = client.post("/api/invitations/send", headers=h_rakesh, json={
        "recipient_email": "rohit@vtu.ac.in",
        "problem_id": 1,
        "duration_mins": 30
    })
    assert dup_res.status_code == 409

    # Only intended recipient (Rohit) can accept/reject
    unauth_acc = client.post(f"/api/invitations/{inv_uid}/accept", headers=h_rakesh)
    assert unauth_acc.status_code == 403

    # 2. Rohit logs in later (offline delivery) and sees persistent notification
    rohit_inbox = client.get("/api/invitations/inbox", headers=h_rohit).json()
    assert rohit_inbox["unread_count"] >= 1
    assert any(n["invitation_uid"] == inv_uid and n["notif_type"] == "CHALLENGE_INVITE" for n in rohit_inbox["notifications"])

    # 3. Rohit clicks ACCEPT CHALLENGE -> creates shared competition session
    acc_res = client.post(f"/api/invitations/{inv_uid}/accept", headers=h_rohit)
    assert acc_res.status_code == 200
    comp_uid = acc_res.json()["competition_uid"]
    assert comp_uid

    # 4. Rakesh receives acceptance notification
    rakesh_inbox = client.get("/api/invitations/inbox", headers=h_rakesh).json()
    assert any(n["invitation_uid"] == inv_uid and n["notif_type"] == "CHALLENGE_ACCEPTED" for n in rakesh_inbox["notifications"])

    # 5. Both Rakesh and Rohit can enter the same competition session
    s_rakesh = client.get(f"/api/duels/{comp_uid}", headers=h_rakesh)
    s_rohit = client.get(f"/api/duels/{comp_uid}", headers=h_rohit)
    assert s_rakesh.status_code == 200
    assert s_rohit.status_code == 200
    assert s_rakesh.json()["problem"]["id"] == s_rohit.json()["problem"]["id"] == 1

    # 6. Test Rejection flow (no competition session created)
    send2 = client.post("/api/invitations/send", headers=h_rakesh, json={
        "recipient_email": "rohit@vtu.ac.in",
        "problem_id": 2,
        "duration_mins": 15
    }).json()
    rej_res = client.post(f"/api/invitations/{send2['invitation_uid']}/reject", headers=h_rohit)
    assert rej_res.status_code == 200
    assert rej_res.json()["status"] == "REJECTED"
