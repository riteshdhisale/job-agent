"""
The public demo's access gate: a visitor types in their email, gets a one-time
magic link, clicks it, and gets a capped, sandboxed session against a sample
profile -- never Ritesh's real resume, job history, or API quota beyond a
small, per-email cap.

This module is only ever imported/used when DEMO_MODE=1 (see app.py). It does
three things, each backed by one small SQLite table in its own file
(data/demo_access.db -- deliberately separate from data/jobs.db and
data/demo_jobs.db, so wiping one never touches the others):

  1. request_access(email, ip)  -- rate-limited (per email AND per IP, since
     an email-only limit doesn't stop someone cycling through throwaway
     addresses to spam the mailbox or the Adzuna/Gemini quota), creates a
     single-use token good for DEMO_LINK_EXPIRY_MINUTES, and emails a magic
     link via Gmail SMTP (an App Password, not the account password --
     see README's "Deploying the public demo" section for how to get one).
  2. verify_token(token) -- marks the token used (so the same link can't be
     replayed) and returns the email it belongs to, or None if it's missing,
     expired, or already used.
  3. discover_runs_remaining(email) / record_discover_run(email) -- the hard
     cap on how many real Discover runs one verified visitor can trigger,
     independent of how many browser sessions/cookies they go through, since
     it's keyed on the email address that had to receive a real link.

Every limit here is intentionally simple (SQLite counters, no external
service) and is a deliberate trade-off for a low-traffic portfolio demo, not
a production abuse-prevention system -- see the docstring on RATE LIMITS
below for exactly what it does and doesn't stop.
"""
from __future__ import annotations

import os
import secrets
import smtplib
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_DB_PATH = DATA_DIR / "demo_access.db"

# --- configuration, all overridable via environment variables at deploy time ---
LINK_EXPIRY_MINUTES = int(os.environ.get("DEMO_LINK_EXPIRY_MINUTES", "30"))
MAX_RUNS_PER_EMAIL = int(os.environ.get("DEMO_MAX_RUNS_PER_EMAIL", "1"))
MAX_REQUESTS_PER_EMAIL_PER_DAY = int(os.environ.get("DEMO_MAX_REQUESTS_PER_EMAIL_PER_DAY", "3"))
MAX_REQUESTS_PER_IP_PER_HOUR = int(os.environ.get("DEMO_MAX_REQUESTS_PER_IP_PER_HOUR", "5"))
# Covers tailor/outreach/prep combined -- the other real LLM-calling actions once a
# visitor has some jobs to act on. One lifetime allowance per email, same "a real link
# had to reach this address" spirit as MAX_RUNS_PER_EMAIL, not a daily reset.
MAX_EXTRA_ACTIONS_PER_EMAIL = int(os.environ.get("DEMO_MAX_EXTRA_ACTIONS_PER_EMAIL", "10"))

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")


class DemoAccessError(Exception):
    """Raised for a request that's rejected before a token is even created
    (rate-limited, bad email) -- distinct from "link expired", which is a
    normal outcome checked at verify time, not a request-time error."""


@dataclass
class RequestResult:
    ok: bool
    message: str


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    db_path = db_path if db_path is not None else DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS access_tokens (
            token TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            ip TEXT,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            used_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discover_usage (
            email TEXT PRIMARY KEY,
            runs_used INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS extra_action_usage (
            email TEXT PRIMARY KEY,
            actions_used INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def _is_plausible_email(email: str) -> bool:
    # Deliberately loose -- this isn't validating deliverability, just filtering
    # out obvious garbage before we bother generating a token or calling SMTP.
    return bool(email) and "@" in email and "." in email.split("@")[-1] and " " not in email


def request_access(email: str, ip: Optional[str], base_url: str,
                    db_path: Optional[Path] = None, _send=None) -> RequestResult:
    """`base_url` is the deployed app's own root URL (Flask's `request.url_root`
    at call time), so the emailed link always points at wherever this instance
    is actually running rather than a hardcoded host. `_send` is a test seam --
    pass a fake sender to avoid real SMTP in tests; production callers omit it
    and get send_magic_link_email.

    RATE LIMITS, and what they don't cover: capped per-email-per-day AND
    per-IP-per-hour, both enforced here before a token is created or an email
    sent. This stops a casual retry loop and a single script hammering the
    endpoint from one IP. It does NOT stop someone with many real inboxes and
    a rotating IP -- that's an accepted, disclosed limitation for a low-traffic
    personal-portfolio demo, not a claim of production-grade abuse prevention."""
    email = (email or "").strip().lower()
    if not _is_plausible_email(email):
        return RequestResult(False, "That doesn't look like a valid email address.")

    now = time.time()
    with closing(_connect(db_path)) as conn:
        if ip:
            recent_from_ip = conn.execute(
                "SELECT COUNT(*) FROM access_tokens WHERE ip = ? AND created_at > ?",
                (ip, now - 3600),
            ).fetchone()[0]
            if recent_from_ip >= MAX_REQUESTS_PER_IP_PER_HOUR:
                return RequestResult(False, "Too many requests from your network in the last hour -- try again later.")

        recent_for_email = conn.execute(
            "SELECT COUNT(*) FROM access_tokens WHERE email = ? AND created_at > ?",
            (email, now - 86400),
        ).fetchone()[0]
        if recent_for_email >= MAX_REQUESTS_PER_EMAIL_PER_DAY:
            return RequestResult(False, "You've already requested a few links today -- check your inbox (and spam folder), or try again tomorrow.")

        token = secrets.token_urlsafe(32)
        expires_at = now + LINK_EXPIRY_MINUTES * 60
        conn.execute(
            "INSERT INTO access_tokens (token, email, ip, created_at, expires_at, used_at) VALUES (?, ?, ?, ?, ?, NULL)",
            (token, email, ip, now, expires_at),
        )
        conn.commit()

    link = f"{base_url.rstrip('/')}/demo/verify/{token}"
    sender = _send or send_magic_link_email
    try:
        sender(email, link)
    except Exception as exc:  # noqa: BLE001 -- surface a clean message, not a raw SMTP traceback
        return RequestResult(False, f"Couldn't send the email ({exc}). Try again in a moment.")

    return RequestResult(True, f"Check {email} for a one-time link -- it's good for {LINK_EXPIRY_MINUTES} minutes.")


def send_magic_link_email(email: str, link: str) -> None:
    """Sends via Gmail's SMTP-over-SSL endpoint using an App Password (NOT the
    account password -- Google blocks plain-password SMTP login entirely, and
    an App Password is scoped to this one use and revocable on its own).
    Getting one: Google Account > Security > 2-Step Verification (must be on)
    > App passwords > create one for "Mail". Set GMAIL_ADDRESS/GMAIL_APP_PASSWORD
    as env vars on whatever host runs this (see README)."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_ADDRESS/GMAIL_APP_PASSWORD not set -- can't send the access email")

    body = (
        "You asked for a look at the job_agent demo.\n\n"
        f"Here's your one-time link (valid {LINK_EXPIRY_MINUTES} minutes, works once):\n"
        f"{link}\n\n"
        "It opens a sandboxed session against a sample resume and a small, capped "
        "discovery run -- nothing here touches anyone's real data.\n\n"
        "Didn't request this? Just ignore it -- the link expires on its own."
    )
    msg = MIMEText(body)
    msg["Subject"] = "Your job_agent demo link"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = email

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [email], msg.as_string())


def verify_token(token: str, db_path: Optional[Path] = None) -> Optional[str]:
    """Single-use: a token that verifies successfully is marked used_at
    immediately, so replaying the same link a second time fails even though
    it hasn't expired yet. Returns the associated email on success, else None
    (covers: token doesn't exist, already used, or past its expiry)."""
    now = time.time()
    with closing(_connect(db_path)) as conn:
        row = conn.execute(
            "SELECT email, expires_at, used_at FROM access_tokens WHERE token = ?", (token,)
        ).fetchone()
        if not row:
            return None
        email, expires_at, used_at = row
        if used_at is not None or now > expires_at:
            return None
        conn.execute("UPDATE access_tokens SET used_at = ? WHERE token = ?", (now, token))
        conn.commit()
        return email


def discover_runs_remaining(email: str, db_path: Optional[Path] = None) -> int:
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT runs_used FROM discover_usage WHERE email = ?", (email,)).fetchone()
        used = row[0] if row else 0
    return max(0, MAX_RUNS_PER_EMAIL - used)


def record_discover_run(email: str, db_path: Optional[Path] = None) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO discover_usage (email, runs_used) VALUES (?, 1) "
            "ON CONFLICT(email) DO UPDATE SET runs_used = runs_used + 1",
            (email,),
        )
        conn.commit()


def extra_actions_remaining(email: str, db_path: Optional[Path] = None) -> int:
    """Tailor/outreach/prep, combined -- see MAX_EXTRA_ACTIONS_PER_EMAIL above."""
    with closing(_connect(db_path)) as conn:
        row = conn.execute("SELECT actions_used FROM extra_action_usage WHERE email = ?", (email,)).fetchone()
        used = row[0] if row else 0
    return max(0, MAX_EXTRA_ACTIONS_PER_EMAIL - used)


def record_extra_action(email: str, db_path: Optional[Path] = None) -> None:
    with closing(_connect(db_path)) as conn:
        conn.execute(
            "INSERT INTO extra_action_usage (email, actions_used) VALUES (?, 1) "
            "ON CONFLICT(email) DO UPDATE SET actions_used = actions_used + 1",
            (email,),
        )
        conn.commit()
