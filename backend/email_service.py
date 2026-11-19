import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Dict, Any

logger = logging.getLogger("placement_tracker.email")


def _load_env_file():
    """Lightweight .env loader so credentials work locally without extra dependencies."""
    for candidate in (".env", "../.env"):
        if os.path.exists(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())


_load_env_file()


def get_smtp_config() -> Dict[str, Any]:
    return {
        "host": os.getenv("SMTP_HOST", "").strip(),
        "port": int(os.getenv("SMTP_PORT", "587") or 587),
        "username": os.getenv("SMTP_USERNAME", "").strip(),
        "password": os.getenv("SMTP_PASSWORD", "").strip(),
        "from_email": os.getenv("SMTP_FROM_EMAIL", "").strip() or os.getenv("SMTP_USERNAME", "").strip(),
        "feedback_to": os.getenv("FEEDBACK_RECEIVER_EMAIL", "rakeskhp2142@gmail.com").strip(),
        "app_url": os.getenv("APP_BASE_URL", "https://cdd-project-1.onrender.com").rstrip("/"),
    }



import json as _json
import urllib.request as _urllib_req

def _send_via_resend_https(to_email: str, subject: str, html_body: str, text_body: str):
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    if not api_key:
        return None
    from_addr = os.getenv("RESEND_FROM_EMAIL", "Placement Tracker <onboarding@resend.dev>").strip()
    payload = _json.dumps({
        "from": from_addr,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }).encode("utf-8")
    req = _urllib_req.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "PlacementTracker/1.0",
        },
        method="POST",
    )
    try:
        with _urllib_req.urlopen(req, timeout=15) as resp:
            if 200 <= resp.status < 300:
                return {"sent": True, "error": None}
            return {"sent": False, "error": f"HTTP {resp.status}"}
    except Exception as exc:
        return {"sent": False, "error": f"{type(exc).__name__}: {exc}"}

def send_email(to_email: str, subject: str, html_body: str, text_body: str = ""):
    resend_res = _send_via_resend_https(to_email, subject, html_body, text_body or html_body)
    if resend_res is not None:
        return resend_res
    """
    Sends an email via SMTP. Never logs passwords.
    Returns {"sent": True} on real SMTP acceptance or {"sent": False, "error": "..."} on failure.
    """
    cfg = get_smtp_config()
    if not cfg["host"] or not cfg["username"] or not cfg["password"]:
        msg = "SMTP is not configured (missing SMTP_HOST, SMTP_USERNAME, or SMTP_PASSWORD)."
        logger.warning("Email skipped to %s: %s", to_email, msg)
        return {"sent": False, "error": msg}

    msg_obj = MIMEMultipart("alternative")
    msg_obj["Subject"] = subject
    msg_obj["From"] = f"Student Placement Tracker <{cfg['from_email']}>"
    msg_obj["To"] = to_email
    msg_obj.attach(MIMEText(text_body, "plain", "utf-8"))
    msg_obj.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if cfg["port"] == 465:
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15) as server:
                server.login(cfg["username"], cfg["password"])
                server.sendmail(cfg["from_email"], [to_email], msg_obj.as_string())
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(cfg["username"], cfg["password"])
                server.sendmail(cfg["from_email"], [to_email], msg_obj.as_string())

        logger.info("Email delivered successfully to %s | Subject: %s", to_email, subject)
        return {"sent": True, "error": None}
    except Exception as exc:
        # Log exception type/message only; never log cfg["password"]
        logger.error("SMTP delivery failed to %s (%s): %s", to_email, type(exc).__name__, str(exc))
        return {"sent": False, "error": f"Email delivery failed: {type(exc).__name__}"}


def send_peer_challenge_email(
    recipient_email: str,
    recipient_name: str,
    challenger_name: str,
    problem_title: str,
    difficulty: str,
    duration_mins: int,
    invitation_uid: str,
) -> Dict[str, Any]:
    cfg = get_smtp_config()
    login_link = f"{cfg['app_url']}/?invite={invitation_uid}"
    subject = f"[Peer Coding Challenge] {challenger_name} challenged you to a 1v1 duel!"

    text_body = (
        f"Hi {recipient_name},\n\n"
        f"{challenger_name} has challenged you to a 1v1 Peer Coding Duel on Student Placement Tracker!\n\n"
        f"Challenge Details:\n"
        f"- Problem: {problem_title}\n"
        f"- Difficulty: {difficulty}\n"
        f"- Time Limit: {duration_mins} minutes\n\n"
        f"Log in to accept or reject the challenge:\n{login_link}\n"
    )

    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 560px; margin: auto; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px;">
      <h2 style="color: #1e293b; margin-top: 0;">⚔️ 1v1 Peer Coding Challenge</h2>
      <p>Hi <strong>{recipient_name}</strong>,</p>
      <p><strong>{challenger_name}</strong> has invited you to a live 1v1 coding duel on <strong>Student Placement Tracker</strong>.</p>
      <table style="width: 100%; border-collapse: collapse; margin: 16px 0; background: #f8fafc;">
        <tr><td style="padding: 8px; border: 1px solid #e2e8f0;"><strong>Challenge</strong></td><td style="padding: 8px; border: 1px solid #e2e8f0;">{problem_title}</td></tr>
        <tr><td style="padding: 8px; border: 1px solid #e2e8f0;"><strong>Difficulty</strong></td><td style="padding: 8px; border: 1px solid #e2e8f0;">{difficulty}</td></tr>
        <tr><td style="padding: 8px; border: 1px solid #e2e8f0;"><strong>Time Limit</strong></td><td style="padding: 8px; border: 1px solid #e2e8f0;">{duration_mins} minutes</td></tr>
      </table>
      <p style="text-align: center; margin: 24px 0;">
        <a href="{login_link}" style="background: #2563eb; color: #ffffff; padding: 12px 22px; text-decoration: none; border-radius: 6px; font-weight: bold;">
          Open Platform to Accept or Reject
        </a>
      </p>
      <p style="font-size: 12px; color: #64748b;">If the button doesn't work, open: {login_link}</p>
    </div>
    """
    return send_email(recipient_email, subject, html_body, text_body)


def send_challenge_status_email(
    challenger_email: str,
    challenger_name: str,
    opponent_name: str,
    status: str,
    problem_title: str,
) -> Dict[str, Any]:
    cfg = get_smtp_config()
    status_label = status.upper()
    subject = f"[Peer Coding Challenge] {opponent_name} {status_label} your challenge"
    text_body = (
        f"Hi {challenger_name},\n\n"
        f"{opponent_name} has {status_label} your coding challenge ({problem_title}).\n"
        f"Open the platform to view details: {cfg['app_url']}\n"
    )
    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 560px; margin: auto; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px;">
      <h3 style="color: #1e293b;">Challenge Status Update: {status_label}</h3>
      <p>Hi <strong>{challenger_name}</strong>,</p>
      <p><strong>{opponent_name}</strong> has <strong>{status_label}</strong> your challenge for <em>{problem_title}</em>.</p>
      <p><a href="{cfg['app_url']}">Return to Student Placement Tracker</a></p>
    </div>
    """
    return send_email(challenger_email, subject, html_body, text_body)


def send_feedback_email(
    student_name: str,
    student_email: str,
    category: str,
    title: str,
    description: str,
    page_name: str = "General",
    submitted_at: str | None = None,
) -> Dict[str, Any]:
    cfg = get_smtp_config()
    receiver = cfg["feedback_to"] or "rakeskhp2142@gmail.com"
    timestamp = submitted_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    norm_cat = "Bug Report" if "bug" in category.lower() else "Suggestion"
    subject = f"[Placement Tracker {norm_cat}] - {title}"

    text_body = (
        f"New {norm_cat} Submitted on Student Placement Tracker\n"
        f"{'=' * 52}\n"
        f"Student Name   : {student_name or 'Anonymous'}\n"
        f"Student Email  : {student_email or 'Not provided'}\n"
        f"Category       : {norm_cat}\n"
        f"Page / Feature : {page_name or 'Not specified'}\n"
        f"Submitted At   : {timestamp}\n"
        f"Title          : {title}\n\n"
        f"Full Description:\n{description}\n"
    )

    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; border: 1px solid #cbd5e1; border-radius: 8px; padding: 20px;">
      <h2 style="color: #0f172a; margin-top: 0;">Placement Tracker — {norm_cat}</h2>
      <table style="width: 100%; border-collapse: collapse; margin-bottom: 16px;">
        <tr><td style="padding: 8px; border: 1px solid #e2e8f0; width: 150px;"><strong>Student Name</strong></td><td style="padding: 8px; border: 1px solid #e2e8f0;">{student_name or 'Anonymous'}</td></tr>
        <tr><td style="padding: 8px; border: 1px solid #e2e8f0;"><strong>Student Email</strong></td><td style="padding: 8px; border: 1px solid #e2e8f0;">{student_email or 'Not provided'}</td></tr>
        <tr><td style="padding: 8px; border: 1px solid #e2e8f0;"><strong>Category</strong></td><td style="padding: 8px; border: 1px solid #e2e8f0;">{norm_cat}</td></tr>
        <tr><td style="padding: 8px; border: 1px solid #e2e8f0;"><strong>Page / Feature</strong></td><td style="padding: 8px; border: 1px solid #e2e8f0;">{page_name or 'Not specified'}</td></tr>
        <tr><td style="padding: 8px; border: 1px solid #e2e8f0;"><strong>Submitted At</strong></td><td style="padding: 8px; border: 1px solid #e2e8f0;">{timestamp}</td></tr>
        <tr><td style="padding: 8px; border: 1px solid #e2e8f0;"><strong>Title</strong></td><td style="padding: 8px; border: 1px solid #e2e8f0;"><strong>{title}</strong></td></tr>
      </table>
      <div style="background: #f8fafc; border-left: 4px solid #2563eb; padding: 12px; white-space: pre-wrap;">{description}</div>
    </div>
    """
    return send_email(receiver, subject, html_body, text_body)
