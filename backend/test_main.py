from fastapi.testclient import TestClient
from main import app, init_db

init_db()
client = TestClient(app)


def test_achievement_dashboard_and_unlock_workflow():
    login = client.post("/api/auth/login", json={"email": "rakesh@vtu.ac.in", "password": "rakesh123"})
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    # 1. Achievement Dashboard returns circular metrics, 8 achievements, 10-axis radar & topic cards
    dash = client.get("/api/coding/achievement-dashboard", headers=headers).json()
    assert len(dash["achievements"]) == 8
    assert len(dash["radar"]) == 10
    assert len(dash["topics"]) == 23
    assert len(dash["heatmap"]) == 30

    # 2. All 15 programming languages are returned for the workspace dropdown
    langs = client.get("/api/languages", headers=headers).json()
    assert len(langs) == 15

    # 3. Run Code never marks problem solved; Submit Code with passing code marks Solved & unlocks FIRST_STEP
    probs = client.get("/api/coding/problems?topic=Arrays&limit=5", headers=headers).json()["problems"]
    p1 = probs[0]

    run_res = client.post("/api/coding/execute", headers=headers, json={
        "problem_id": p1["id"],
        "language": "python",
        "code": "import sys\nd=sys.stdin.read().split()\nn=int(d[0])\nprint(len(set(map(int,d[1:1+n]))))",
        "mode": "run",
        "time_taken_sec": 40
    }).json()
    assert run_res["marked_solved"] is False

    sub_res = client.post("/api/coding/execute", headers=headers, json={
        "problem_id": p1["id"],
        "language": "python",
        "code": "import sys\nd=sys.stdin.read().split()\nn=int(d[0])\nprint(len(set(map(int,d[1:1+n]))))",
        "mode": "submit",
        "time_taken_sec": 40
    }).json()
    assert sub_res["verdict"] == "ACCEPTED"
    assert sub_res["marked_solved"] is True


def test_competitive_and_custom_input():
    login = client.post("/api/auth/login", json={"email": "rakesh@vtu.ac.in", "password": "rakesh123"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    dc = client.get("/api/competitive/daily-challenge", headers=headers)
    assert dc.status_code == 200
    assert "problem" in dc.json()

    crun = client.post("/api/competitive/custom-run", headers=headers, json={
        "language": "python",
        "code": "print(int(input()) * 2)",
        "custom_input": "21"
    })
    assert crun.status_code == 200
    assert crun.json()["output"] == "42"

    lb = client.get("/api/competitive/leaderboard", headers=headers)
    assert lb.status_code == 200
    assert len(lb.json()) >= 1
