"""Tests for ProfileStore and JobStore -- the persistent state everything
else in this package depends on. No API key or network needed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from profile_store import CandidateProfile, ProfileStore
from job_store import JobRecord, JobStore, STATUSES
from tools.extract_tool import JobListing
from tools.score_tool import MatchResult


def make_match(title="Product Manager", score=80, posted_at=None, salary_min=None, salary_max=None):
    listing = JobListing(title=title, location="Remote", url="http://x/job", snippet="a role", source_url="http://x",
                          posted_at=posted_at, salary_min=salary_min, salary_max=salary_max)
    return MatchResult(job=listing, score=score, rationale="test rationale",
                        matched_skills=["product"], missing_skills=["sql"])


def test_profile_roundtrip(tmp_path):
    path = tmp_path / "profile.json"
    store = ProfileStore(path=path)

    profile = store.load()  # doesn't exist yet -- should return a blank profile, not crash
    assert profile.full_name == ""

    profile.full_name = "Test Person"
    profile.remember("Are you authorized to work here?", "Yes")
    store.save(profile)

    reloaded = store.load()
    assert reloaded.full_name == "Test Person"
    assert reloaded.get("Are you authorized to work here?") == "Yes"
    # normalization: differently-cased/spaced version of the same question should still hit the cache
    assert reloaded.get("  ARE YOU authorized to work here?  ") == "Yes"


def test_job_store_add_and_get(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    job_id = store.add_match(make_match())
    record = store.get(job_id)
    assert record.title == "Product Manager"
    assert record.status == "found"
    assert record.source_type == "career_site"
    assert record.application_method == "web_form"


def test_job_store_linkedin_lead_captures_contact(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    job_id = store.add_match(make_match(), source_type="manual_lead", application_method="email",
                              application_target="jobs@example.com", poster_name="Alex Rivera",
                              poster_title="Director of Product")
    record = store.get(job_id)
    assert record.application_method == "email"
    assert record.outreach_contacts == [{"name": "Alex Rivera", "title": "Director of Product", "source": "linkedin_post"}]


def test_job_store_list_sorted_by_score_desc(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    store.add_match(make_match(score=10))
    store.add_match(make_match(score=90))
    store.add_match(make_match(score=50))
    scores = [j.score for j in store.list()]
    assert scores == [90, 50, 10]


def test_job_store_status_lifecycle(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    job_id = store.add_match(make_match())
    for status in STATUSES:
        store.set_status(job_id, status)
        assert store.get(job_id).status == status

    with pytest.raises(ValueError):
        store.set_status(job_id, "not_a_real_status")


def test_job_store_filter_by_status(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    id1 = store.add_match(make_match(title="A"))
    id2 = store.add_match(make_match(title="B"))
    store.set_status(id2, "applied")
    assert [j.title for j in store.list(status="found")] == ["A"]
    assert [j.title for j in store.list(status="applied")] == ["B"]


def test_job_store_delete_one(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    id1 = store.add_match(make_match(title="Keep me"))
    id2 = store.add_match(make_match(title="Delete me"))

    assert store.delete(id2) is True
    assert store.get(id2) is None
    assert store.get(id1) is not None
    # deleting again (or an id that never existed) reports nothing happened, doesn't raise
    assert store.delete(id2) is False
    assert store.delete(99999) is False


def test_job_store_delete_all(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    store.add_match(make_match(title="A"))
    store.add_match(make_match(title="B"))
    store.add_match(make_match(title="C"))

    deleted = store.delete_all()
    assert deleted == 3
    assert store.list() == []
    # calling it again on an already-empty store is a no-op, not an error
    assert store.delete_all() == 0


def test_job_store_persists_posted_at_and_salary(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    job_id = store.add_match(make_match(posted_at="2026-09-01T10:00:00Z",
                                         salary_min=2500000, salary_max=3500000))
    record = store.get(job_id)
    assert record.posted_at == "2026-09-01T10:00:00Z"
    assert record.salary_min == 2500000
    assert record.salary_max == 3500000


def test_job_store_missing_posted_at_and_salary_stay_none(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    job_id = store.add_match(make_match())  # no posted_at/salary passed
    record = store.get(job_id)
    assert record.posted_at is None
    assert record.salary_min is None
    assert record.salary_max is None


def test_a_pre_existing_db_without_the_new_columns_gets_migrated(tmp_path):
    """Simulates a jobs.db created before posted_at/salary_min/salary_max existed --
    opening it with the current JobStore should add the missing columns rather than
    crashing on "no such column"."""
    import sqlite3
    db_path = tmp_path / "old_jobs.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, location TEXT, url TEXT, source_url TEXT, snippet TEXT,
                score INTEGER, rationale TEXT, matched_skills TEXT, missing_skills TEXT,
                status TEXT NOT NULL DEFAULT 'found', source_type TEXT NOT NULL DEFAULT 'career_site',
                application_method TEXT NOT NULL DEFAULT 'web_form', application_target TEXT,
                poster_name TEXT, poster_title TEXT, tailored_resume TEXT, outreach_contacts TEXT,
                outreach_message TEXT, application_notes TEXT, interview_notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

    store = JobStore(db_path=db_path)  # should migrate on open, not raise
    job_id = store.add_match(make_match(salary_min=3000000))
    record = store.get(job_id)
    assert record.salary_min == 3000000

    # opening it again (columns already added) should be a harmless no-op too
    JobStore(db_path=db_path)
