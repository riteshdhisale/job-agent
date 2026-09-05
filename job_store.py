"""
Job store -- turns Agent 1's one-shot "print top 10 and forget" output into
something the rest of the system can act on across multiple CLI invocations.
Every discovered job gets a stable id and moves through a status lifecycle
so later commands (tailor, contacts, outreach, apply, prep) can look a job
up by id instead of you re-pasting a URL and description every time.

Status lifecycle (a job doesn't have to visit every stage):
  found -> tailored -> contacted -> ready_to_submit -> applied -> interviewing -> closed
"contacted" means an outreach draft exists, NOT that anything was sent --
this system never sends anything on its own (see 02_business_strategy.md,
section 4). Likewise "ready_to_submit" means an application (web form or
email) was prepared and is waiting for YOU to review and send; "applied" is
set by you afterward via `cli.py mark-applied`, confirming you actually did.

Two sources feed this store: career-site scanning (source_type='career_site',
application_method='web_form', via add_match from Agent 1's pipeline) and
pasted LinkedIn posts (source_type='linkedin_post', application_method one of
email/link/dm/unclear, via linkedin_post_tool.py) -- see cli.py's `discover`
and `add-linkedin-post` commands.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "jobs.db"

STATUSES = ["found", "tailored", "contacted", "ready_to_submit", "applied", "interviewing", "closed"]
APPLICATION_METHODS = ["web_form", "email", "link", "dm", "unclear"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    location TEXT,
    url TEXT,
    source_url TEXT,
    snippet TEXT,
    score INTEGER,
    rationale TEXT,
    matched_skills TEXT,
    missing_skills TEXT,
    status TEXT NOT NULL DEFAULT 'found',
    source_type TEXT NOT NULL DEFAULT 'career_site',
    application_method TEXT NOT NULL DEFAULT 'web_form',
    application_target TEXT,
    poster_name TEXT,
    poster_title TEXT,
    tailored_resume TEXT,
    outreach_contacts TEXT,
    outreach_message TEXT,
    application_notes TEXT,
    interview_notes TEXT,
    posted_at TEXT,
    salary_min INTEGER,
    salary_max INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# Added after the original schema shipped -- a plain CREATE TABLE IF NOT EXISTS
# doesn't add columns to a jobs.db that already exists from before this feature,
# so existing users' databases need this run once. Harmless/no-op if the columns
# are already there (fresh installs get them from SCHEMA above directly).
_MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN posted_at TEXT",
    "ALTER TABLE jobs ADD COLUMN salary_min INTEGER",
    "ALTER TABLE jobs ADD COLUMN salary_max INTEGER",
]


@dataclass
class JobRecord:
    id: int
    title: str
    location: str
    url: str
    source_url: str
    snippet: str
    score: int
    rationale: str
    matched_skills: List[str]
    missing_skills: List[str]
    status: str
    source_type: str
    application_method: str
    application_target: Optional[str] = None
    poster_name: Optional[str] = None
    poster_title: Optional[str] = None
    tailored_resume: Optional[str] = None
    outreach_contacts: Optional[List[Dict[str, Any]]] = None
    outreach_message: Optional[str] = None
    application_notes: Optional[str] = None
    interview_notes: Optional[str] = None
    posted_at: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None


class JobStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(SCHEMA)
            for migration in _MIGRATIONS:
                try:
                    conn.execute(migration)
                except sqlite3.OperationalError:
                    pass  # column already exists -- this db predates the migration, or is already current
            conn.commit()

    def add_match(self, match, source_type: str = "career_site", application_method: str = "web_form",
                  application_target: str = "", poster_name: str = "", poster_title: str = "") -> int:
        """Insert a tools.score_tool.MatchResult. Returns the new job id.
        Inserts every match, not just the top N -- filtering/ranking happens
        at read time via `list()`, so nothing found is silently discarded."""
        outreach_contacts = None
        if poster_name:
            # A LinkedIn post's author is very often the actual hiring
            # manager -- capture them as a ready-made contact so Agent 3's
            # `contacts` step can skip the Apollo lookup entirely for this job.
            outreach_contacts = json.dumps([{"name": poster_name, "title": poster_title, "source": "linkedin_post"}])

        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.execute(
                "INSERT INTO jobs (title, location, url, source_url, snippet, score, rationale, "
                "matched_skills, missing_skills, status, source_type, application_method, "
                "application_target, poster_name, poster_title, outreach_contacts, "
                "posted_at, salary_min, salary_max) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (match.job.title, match.job.location, match.job.url, match.job.source_url,
                 match.job.snippet, match.score, match.rationale,
                 json.dumps(match.matched_skills), json.dumps(match.missing_skills), "found",
                 source_type, application_method, application_target or None,
                 poster_name or None, poster_title or None, outreach_contacts,
                 getattr(match.job, "posted_at", None), getattr(match.job, "salary_min", None),
                 getattr(match.job, "salary_max", None)),
            )
            conn.commit()
            return cur.lastrowid

    def get(self, job_id: int) -> Optional[JobRecord]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return self._row_to_record(row) if row else None

    def list(self, status: Optional[str] = None) -> List[JobRecord]:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            if status:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY score DESC", (status,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM jobs ORDER BY score DESC").fetchall()
            return [self._row_to_record(r) for r in rows]

    def update(self, job_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [job_id]
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", values)
            conn.commit()

    def set_status(self, job_id: int, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status '{status}', must be one of {STATUSES}")
        self.update(job_id, status=status)

    def delete(self, job_id: int) -> bool:
        """Permanently remove a job (e.g. a duplicate, a stale/test listing, or one you're
        no longer interested in). Returns True if a row was actually deleted."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()
            return cur.rowcount > 0

    def delete_all(self) -> int:
        """Wipe every job -- a full reset. Returns how many rows were removed."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            cur = conn.execute("DELETE FROM jobs")
            conn.commit()
            return cur.rowcount

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> JobRecord:
        d = dict(row)
        return JobRecord(
            id=d["id"], title=d["title"], location=d["location"], url=d["url"],
            source_url=d["source_url"], snippet=d["snippet"], score=d["score"], rationale=d["rationale"],
            matched_skills=json.loads(d["matched_skills"] or "[]"),
            missing_skills=json.loads(d["missing_skills"] or "[]"),
            status=d["status"], source_type=d["source_type"], application_method=d["application_method"],
            application_target=d["application_target"], poster_name=d["poster_name"], poster_title=d["poster_title"],
            tailored_resume=d["tailored_resume"],
            outreach_contacts=json.loads(d["outreach_contacts"]) if d["outreach_contacts"] else None,
            outreach_message=d["outreach_message"], application_notes=d["application_notes"],
            interview_notes=d["interview_notes"],
            posted_at=d.get("posted_at"), salary_min=d.get("salary_min"), salary_max=d.get("salary_max"),
        )
