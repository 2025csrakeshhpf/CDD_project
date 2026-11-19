import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from main import DB_PATH, decode_token, get_current_user, hash_password
from models import User

router = APIRouter(tags=["Peer Challenge Invitations & Real-Time Notifications"])

ALLOWED_DURATIONS = {15, 30, 45, 60}
DEFAULT_EXPIRY_MINUTES = 60


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_invitation_tables():
    conn = _conn()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS challenge_invitations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invitation_uid TEXT UNIQUE NOT NULL,
            sender_id INTEGER NOT NULL,
            sender_email TEXT NOT NULL,
            sender_name TEXT NOT NULL,
            sender_usn TEXT DEFAULT '',
            receiver_id INTEGER NOT NULL,
            receiver_email TEXT NOT NULL,
            receiver_name TEXT NOT NULL,
            problem_id INTEGER NOT NULL,
            problem_title TEXT NOT NULL,
            problem_difficulty TEXT DEFAULT 'Easy',
            duration_mins INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            competition_uid TEXT DEFAULT '',
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            responded_at TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS user_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_id INTEGER NOT NULL,
            recipient_email TEXT NOT NULL,
            sender_name TEXT NOT NULL,
            sender_profile TEXT DEFAULT '',
            invitation_uid TEXT DEFAULT '',
            notif_type TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            problem_title TEXT DEFAULT '',
            duration_mins INTEGER DEFAULT 30,
            is_read INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """
    )

    cur.execute("SELECT id FROM users WHERE LOWER(TRIM(email)) = ?", ("rohit@vtu.ac.in",))
    if not cur.fetchone():
        cur.execute(
            """
            INSERT INTO users (name, usn, college, branch, email, password_hash, role, preferred_language, daily_goal_problems, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'student', 'python', 5, ?)
            """,
            ("Rohit", "4IK25CS162", "Visvesvaraya Technological University (VTU)", "Computer Science Engineering", "rohit@vtu.ac.in", hash_password("rohit123"), datetime.utcnow().isoformat()),
        )

    conn.commit()
    conn.close()


init_invitation_tables()


class NotificationHub:
    def __init__(self):
        self.sockets_by_user_id: Dict[int, List[WebSocket]] = {}

    async def connect(self, user_id: int, ws: WebSocket):
        await ws.accept()
        self.sockets_by_user_id.setdefault(user_id, []).append(ws)

    def disconnect(self, user_id: int, ws: WebSocket):
        if user_id in self.sockets_by_user_id and ws in self.sockets_by_user_id[user_id]:
            self.sockets_by_user_id[user_id].remove(ws)

    async def push_to_user_id(self, user_id: int, payload: Dict) -> bool:
        sockets = self.sockets_by_user_id.get(user_id, [])
        if not sockets:
            return False
        delivered = False
        dead = []
        for ws in sockets:
            try:
                await ws.send_json(payload)
                delivered = True
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(user_id, ws)
        return delivered


notif_hub = NotificationHub()


def _expire_stale_invitations(conn: sqlite3.Connection):
    now_iso = datetime.utcnow().isoformat()
    cur = conn.cursor()
    cur.execute("UPDATE challenge_invitations SET status = 'EXPIRED' WHERE status = 'PENDING' AND expires_at <= ?", (now_iso,))
    conn.commit()


class SendInvitationReq(BaseModel):
    recipient_email: str
    problem_id: int = 1
    duration_mins: int = 30
    expiry_minutes: int = DEFAULT_EXPIRY_MINUTES


@router.post("/api/invitations/send")
async def send_challenge_invitation(req: SendInvitationReq, current_user: User = Depends(get_current_user)):
    recipient_email = req.recipient_email.strip().lower()
    sender_email = current_user.email.strip().lower()

    if recipient_email == sender_email:
        raise HTTPException(status_code=400, detail="You cannot challenge yourself.")
    if req.duration_mins not in ALLOWED_DURATIONS:
        raise HTTPException(status_code=400, detail="Duration must be 15, 30, 45, or 60 minutes.")

    conn = _conn()
    _expire_stale_invitations(conn)
    cur = conn.cursor()

    # Resolve Sender exact database user ID
    cur.execute("SELECT id, name, usn, email FROM users WHERE LOWER(TRIM(email)) = ?", (sender_email,))
    sender_row = cur.fetchone()
    if not sender_row:
        conn.close()
        raise HTTPException(status_code=401, detail="Sender account not found.")

    # Resolve Recipient exact database user ID using registered email
    cur.execute("SELECT id, name, usn, email, role FROM users WHERE LOWER(TRIM(email)) = ?", (recipient_email,))
    recipient = cur.fetchone()
    if not recipient or recipient["role"] != "student":
        conn.close()
        raise HTTPException(status_code=404, detail="No registered student found with that email address.")

    if recipient["id"] == sender_row["id"]:
        conn.close()
        raise HTTPException(status_code=400, detail="You cannot challenge yourself.")

    # Resolve coding problem
    cur.execute("SELECT id, title, difficulty FROM coding_problems WHERE id = ?", (req.problem_id,))
    prob = cur.fetchone()
    if not prob:
        cur.execute("SELECT id, title, difficulty FROM coding_problems ORDER BY id ASC LIMIT 1")
        prob = cur.fetchone()

    # Prevent duplicate pending invitations between same sender_id and receiver_id for this problem
    cur.execute(
        """
        SELECT id FROM challenge_invitations
        WHERE sender_id = ? AND receiver_id = ? AND problem_id = ? AND status = 'PENDING'
        """,
        (sender_row["id"], recipient["id"], prob["id"]),
    )
    if cur.fetchone():
        conn.close()
        raise HTTPException(status_code=409, detail="A pending challenge invitation already exists for this student and problem.")

    inv_uid = secrets.token_hex(5).upper()
    now_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    expires_iso = (datetime.utcnow() + timedelta(minutes=max(5, req.expiry_minutes))).isoformat()
    sender_profile = f"{sender_row['name']}"

    cur.execute(
        """
        INSERT INTO challenge_invitations (
            invitation_uid, sender_id, sender_email, sender_name, sender_usn,
            receiver_id, receiver_email, receiver_name, problem_id, problem_title,
            problem_difficulty, duration_mins, status, expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
        """,
        (
            inv_uid, sender_row["id"], sender_row["email"], sender_row["name"], sender_row["usn"] or "",
            recipient["id"], recipient["email"], recipient["name"], prob["id"], prob["title"],
            prob["difficulty"], req.duration_mins, expires_iso, now_local,
        ),
    )

    notif_msg = f"{sender_row['name']} challenged you to '{prob['title']}' ({req.duration_mins} mins)."
    cur.execute(
        """
        INSERT INTO user_notifications (
            recipient_id, recipient_email, sender_name, sender_profile, invitation_uid,
            notif_type, title, message, problem_title, duration_mins, is_read, created_at
        ) VALUES (?, ?, ?, ?, ?, 'CHALLENGE_INVITE', 'New Coding Challenge Invitation', ?, ?, ?, 0, ?)
        """,
        (
            recipient["id"], recipient["email"], sender_row["name"], sender_profile, inv_uid,
            notif_msg, prob["title"], req.duration_mins, now_local,
        ),
    )
    notif_id = cur.lastrowid
    conn.commit()
    conn.close()

    # Deliver immediately over WebSocket if recipient is online; persisted in SQLite if offline
    delivered_live = await notif_hub.push_to_user_id(
        recipient["id"],
        {
            "event": "NEW_NOTIFICATION",
            "notification": {
                "id": notif_id,
                "recipient_id": recipient["id"],
                "invitation_uid": inv_uid,
                "notif_type": "CHALLENGE_INVITE",
                "sender_name": sender_row["name"],
                "problem_title": prob["title"],
                "duration_mins": req.duration_mins,
                "message": notif_msg,
                "created_at": now_local,
                "is_read": 0,
            },
        },
    )

    return {
        "status": "PENDING",
        "invitation_uid": inv_uid,
        "recipient_id": recipient["id"],
        "recipient_name": recipient["name"],
        "recipient_email": recipient["email"],
        "problem_title": prob["title"],
        "duration_mins": req.duration_mins,
        "delivered_live": delivered_live,
        "message": f"Invitation sent to {recipient['name']} ({recipient['email']})!",
    }


@router.get("/api/invitations/inbox")
def get_invitations_and_notifications(current_user: User = Depends(get_current_user)):
    conn = _conn()
    _expire_stale_invitations(conn)
    cur = conn.cursor()

    cur.execute("SELECT id, email FROM users WHERE LOWER(TRIM(email)) = ?", (current_user.email.strip().lower(),))
    u_row = cur.fetchone()
    uid = u_row["id"] if u_row else -1
    u_email = (u_row["email"] if u_row else current_user.email).strip().lower()

    cur.execute(
        """
        SELECT * FROM user_notifications
        WHERE recipient_id = ? OR LOWER(TRIM(recipient_email)) = ?
        ORDER BY id DESC LIMIT 50
        """,
        (uid, u_email),
    )
    notifications = [dict(r) for r in cur.fetchall()]
    unread_count = sum(1 for n in notifications if n["is_read"] == 0)

    cur.execute(
        """
        SELECT * FROM challenge_invitations
        WHERE sender_id = ? OR receiver_id = ? OR LOWER(TRIM(sender_email)) = ? OR LOWER(TRIM(receiver_email)) = ?
        ORDER BY id DESC LIMIT 50
        """,
        (uid, uid, u_email, u_email),
    )
    history = [dict(r) for r in cur.fetchall()]
    conn.close()

    return {
        "unread_count": unread_count,
        "notifications": notifications,
        "history": history,
    }


@router.post("/api/invitations/{invitation_uid}/accept")
async def accept_challenge_invitation(invitation_uid: str, current_user: User = Depends(get_current_user)):
    conn = _conn()
    _expire_stale_invitations(conn)
    cur = conn.cursor()

    cur.execute("SELECT id, name, email FROM users WHERE LOWER(TRIM(email)) = ?", (current_user.email.strip().lower(),))
    u_row = cur.fetchone()
    if not u_row:
        conn.close()
        raise HTTPException(status_code=401, detail="User not found.")

    cur.execute("SELECT * FROM challenge_invitations WHERE invitation_uid = ?", (invitation_uid,))
    inv = cur.fetchone()
    if not inv:
        conn.close()
        raise HTTPException(status_code=404, detail="Invitation not found.")
    if inv["receiver_id"] != u_row["id"] and inv["receiver_email"].strip().lower() != u_row["email"].strip().lower():
        conn.close()
        raise HTTPException(status_code=403, detail="Only the invited recipient can accept this invitation.")
    if inv["status"] != "PENDING":
        conn.close()
        raise HTTPException(status_code=400, detail=f"Invitation is already {inv['status']}.")

    comp_uid = secrets.token_hex(4).upper()
    now_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute(
        """
        INSERT INTO duel_challenges (
            challenge_uid, challenger_email, challenger_name, opponent_email, opponent_name,
            problem_id, difficulty_pref, duration_mins, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Accepted', ?)
        """,
        (
            comp_uid, inv["sender_email"], inv["sender_name"],
            inv["receiver_email"], inv["receiver_name"],
            inv["problem_id"], inv["problem_difficulty"], inv["duration_mins"], now_local,
        ),
    )

    cur.execute(
        "UPDATE challenge_invitations SET status = 'ACCEPTED', competition_uid = ?, responded_at = ? WHERE id = ?",
        (comp_uid, now_local, inv["id"]),
    )
    cur.execute(
        "UPDATE user_notifications SET is_read = 1 WHERE invitation_uid = ? AND (recipient_id = ? OR LOWER(TRIM(recipient_email)) = ?)",
        (invitation_uid, u_row["id"], u_row["email"].strip().lower()),
    )

    accept_msg = f"{u_row['name']} ACCEPTED your {inv['duration_mins']}-min coding challenge on '{inv['problem_title']}'!"
    cur.execute(
        """
        INSERT INTO user_notifications (
            recipient_id, recipient_email, sender_name, sender_profile, invitation_uid,
            notif_type, title, message, problem_title, duration_mins, is_read, created_at
        ) VALUES (?, ?, ?, ?, ?, 'CHALLENGE_ACCEPTED', 'Challenge Accepted!', ?, ?, ?, 0, ?)
        """,
        (
            inv["sender_id"], inv["sender_email"], u_row["name"], u_row["name"], invitation_uid,
            accept_msg, inv["problem_title"], inv["duration_mins"], now_local,
        ),
    )
    sender_notif_id = cur.lastrowid
    conn.commit()
    conn.close()

    await notif_hub.push_to_user_id(
        inv["sender_id"],
        {
            "event": "INVITATION_ACCEPTED",
            "competition_uid": comp_uid,
            "invitation_uid": invitation_uid,
            "notification": {
                "id": sender_notif_id,
                "invitation_uid": invitation_uid,
                "competition_uid": comp_uid,
                "notif_type": "CHALLENGE_ACCEPTED",
                "sender_name": u_row["name"],
                "message": accept_msg,
                "created_at": now_local,
                "is_read": 0,
            },
        },
    )

    return {
        "status": "ACCEPTED",
        "invitation_uid": invitation_uid,
        "competition_uid": comp_uid,
        "problem_id": inv["problem_id"],
        "duration_mins": inv["duration_mins"],
    }


@router.post("/api/invitations/{invitation_uid}/reject")
async def reject_challenge_invitation(invitation_uid: str, current_user: User = Depends(get_current_user)):
    conn = _conn()
    _expire_stale_invitations(conn)
    cur = conn.cursor()

    cur.execute("SELECT id, name, email FROM users WHERE LOWER(TRIM(email)) = ?", (current_user.email.strip().lower(),))
    u_row = cur.fetchone()
    if not u_row:
        conn.close()
        raise HTTPException(status_code=401, detail="User not found.")

    cur.execute("SELECT * FROM challenge_invitations WHERE invitation_uid = ?", (invitation_uid,))
    inv = cur.fetchone()
    if not inv:
        conn.close()
        raise HTTPException(status_code=404, detail="Invitation not found.")
    if inv["receiver_id"] != u_row["id"] and inv["receiver_email"].strip().lower() != u_row["email"].strip().lower():
        conn.close()
        raise HTTPException(status_code=403, detail="Only the invited recipient can reject this invitation.")
    if inv["status"] != "PENDING":
        conn.close()
        raise HTTPException(status_code=400, detail=f"Invitation is already {inv['status']}.")

    now_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("UPDATE challenge_invitations SET status = 'REJECTED', responded_at = ? WHERE id = ?", (now_local, inv["id"]))
    cur.execute(
        "UPDATE user_notifications SET is_read = 1 WHERE invitation_uid = ? AND (recipient_id = ? OR LOWER(TRIM(recipient_email)) = ?)",
        (invitation_uid, u_row["id"], u_row["email"].strip().lower()),
    )

    reject_msg = f"{u_row['name']} rejected your coding challenge invitation on '{inv['problem_title']}'."
    cur.execute(
        """
        INSERT INTO user_notifications (
            recipient_id, recipient_email, sender_name, sender_profile, invitation_uid,
            notif_type, title, message, problem_title, duration_mins, is_read, created_at
        ) VALUES (?, ?, ?, ?, ?, 'CHALLENGE_REJECTED', 'Challenge Rejected', ?, ?, ?, 0, ?)
        """,
        (
            inv["sender_id"], inv["sender_email"], u_row["name"], u_row["name"], invitation_uid,
            reject_msg, inv["problem_title"], inv["duration_mins"], now_local,
        ),
    )
    sender_notif_id = cur.lastrowid
    conn.commit()
    conn.close()

    await notif_hub.push_to_user_id(
        inv["sender_id"],
        {
            "event": "INVITATION_REJECTED",
            "invitation_uid": invitation_uid,
            "notification": {
                "id": sender_notif_id,
                "invitation_uid": invitation_uid,
                "notif_type": "CHALLENGE_REJECTED",
                "sender_name": u_row["name"],
                "message": reject_msg,
                "created_at": now_local,
                "is_read": 0,
            },
        },
    )

    return {"status": "REJECTED", "invitation_uid": invitation_uid}


@router.post("/api/notifications/{notif_id}/read")
def mark_notification_read(notif_id: int, current_user: User = Depends(get_current_user)):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT id, email FROM users WHERE LOWER(TRIM(email)) = ?", (current_user.email.strip().lower(),))
    u_row = cur.fetchone()
    uid = u_row["id"] if u_row else -1
    cur.execute(
        "UPDATE user_notifications SET is_read = 1 WHERE id = ? AND (recipient_id = ? OR LOWER(TRIM(recipient_email)) = ?)",
        (notif_id, uid, current_user.email.strip().lower()),
    )
    conn.commit()
    conn.close()
    return {"status": "read"}


@router.websocket("/ws/notifications")
async def user_notifications_ws(ws: WebSocket, token: str = ""):
    try:
        claims = decode_token(token)
        email_norm = claims["email"].strip().lower()
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE LOWER(TRIM(email)) = ?", (email_norm,))
        u_row = cur.fetchone()
        conn.close()
        if not u_row:
            await ws.close(code=1008)
            return
        user_id = u_row["id"]
    except Exception:
        await ws.close(code=1008)
        return

    await notif_hub.connect(user_id, ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        notif_hub.disconnect(user_id, ws)
