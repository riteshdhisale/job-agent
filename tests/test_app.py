"""
Tests for the Flask web app's JSON API routes -- previously zero automated
coverage here (a documented, long-standing gap). This exercises app.py's own
glue logic (routing, request/response shape, background-thread discovery
state) using Flask's test client and the default `fake` engine throughout.
It deliberately does NOT re-test business logic that already has its own
tests elsewhere (tailor_tool's actual tailoring, apply_tool's Playwright
form-filling, run_discovery's source-by-source behavior) -- those get
monkeypatched to fast, canned stand-ins so this file stays fast and only
checks that app.py wires into them correctly and returns what the
dashboard's JS (templates/index.html) expects.

Every test uses a JobStore pointed at a temp database (via monkeypatching
app.JobStore) so nothing here ever touches the real data/jobs.db.
"""
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app as app_module
from job_store import JobStore as RealJobStore
from tools.apply_tool import ApplyResult, FilledField
from tools.extract_tool import JobListing
from tools.score_tool import MatchResult
from tools.tailor_tool import MIN_MATCH_SCORE_TO_TAILOR


@pytest.fixture(autouse=True)
def reset_discover_state():
    """_discover_state is a module-level dict shared across requests (that's the
    whole point -- it's how the progress panel polls) but that also means it leaks
    between tests unless reset."""
    app_module._discover_state.update(running=False, done=0, total=0, label="", jobs_found=0,
                                       error=None, finished=False, summary=None)
    yield
    app_module._discover_state.update(running=False, done=0, total=0, label="", jobs_found=0,
                                       error=None, finished=False, summary=None)


@pytest.fixture
def store(tmp_path, monkeypatch):
    db_path = tmp_path / "jobs.db"
    real_store = RealJobStore(db_path=db_path)
    monkeypatch.setattr(app_module, "JobStore", lambda *a, **kw: real_store)
    return real_store


@pytest.fixture
def client(store):
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def _seed(store, title="Product Manager", score=80, **listing_kwargs):
    defaults = dict(title=title, location="Remote", url="http://x/job", snippet="a real role",
                     source_url="http://x")
    defaults.update(listing_kwargs)
    listing = JobListing(**defaults)
    match = MatchResult(job=listing, score=score, rationale="strong fit",
                         matched_skills=["product"], missing_skills=["sql"])
    return store.add_match(match)


# --- dashboard page ----------------------------------------------------------

def test_index_page_loads(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"job_agent" in resp.data


# --- /api/jobs ----------------------------------------------------------------

def test_api_jobs_empty_store(client):
    resp = client.get("/api/jobs")
    data = resp.get_json()
    assert data["jobs"] == []
    assert data["total"] == 0
    assert data["applied_or_further"] == 0


def test_api_jobs_includes_computed_display_fields(client, store):
    job_id = _seed(store, salary_min=2_500_000, salary_max=3_500_000,
                    posted_at="2026-09-04T00:00:00Z", url="http://x/real-posting")
    resp = client.get("/api/jobs")
    job = resp.get_json()["jobs"][0]
    assert job["id"] == job_id
    assert "L" in job["salary_display"]  # e.g. "₹25L–₹35L" -- exact formatting covered in app.py's own tests
    assert job["target_url"] == "http://x/real-posting"


def test_api_jobs_filters_by_status(store, client):
    id_found = _seed(store, title="Still open")
    id_applied = _seed(store, title="Already applied")
    store.set_status(id_applied, "applied")

    resp = client.get("/api/jobs?status=applied")
    titles = [j["title"] for j in resp.get_json()["jobs"]]
    assert titles == ["Already applied"]


# --- /api/jobs/<id> -------------------------------------------------------------

def test_api_job_detail_404_for_missing_job(client):
    resp = client.get("/api/jobs/9999")
    assert resp.status_code == 404
    assert resp.get_json()["ok"] is False


def test_api_job_detail_returns_full_job(store, client):
    job_id = _seed(store)
    resp = client.get(f"/api/jobs/{job_id}")
    data = resp.get_json()
    assert data["ok"] is True
    assert data["job"]["id"] == job_id
    assert data["job"]["matched_skills"] == ["product"]


# --- status changes + delete -----------------------------------------------------

def test_api_job_status_change(store, client):
    job_id = _seed(store)
    resp = client.post(f"/api/jobs/{job_id}/status", json={"status": "applied"})
    assert resp.get_json()["ok"] is True
    assert store.get(job_id).status == "applied"


def test_api_job_status_change_rejects_unknown_status(store, client):
    job_id = _seed(store)
    resp = client.post(f"/api/jobs/{job_id}/status", json={"status": "not_a_real_status"})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
    assert store.get(job_id).status == "found"  # unchanged


def test_api_delete_job(store, client):
    job_id = _seed(store)
    resp = client.post(f"/api/jobs/{job_id}/delete")
    assert resp.get_json()["ok"] is True
    assert store.get(job_id) is None


def test_api_delete_missing_job_reports_not_found(client):
    resp = client.post("/api/jobs/9999/delete")
    assert resp.status_code == 404
    assert resp.get_json()["ok"] is False


def test_api_mark_applied(store, client):
    job_id = _seed(store)
    resp = client.post(f"/api/jobs/{job_id}/mark-applied")
    assert resp.get_json()["status"] == "applied"
    assert store.get(job_id).status == "applied"


# --- tailor (uses the real tailor_tool + FakeLLMClient, no mocking needed) -------

def test_api_tailor_succeeds_above_the_score_gate(store, client):
    job_id = _seed(store, score=MIN_MATCH_SCORE_TO_TAILOR)
    resp = client.post(f"/api/jobs/{job_id}/tailor", json={})
    data = resp.get_json()
    assert data["ok"] is True
    assert data["status"] == "tailored"
    assert store.get(job_id).status == "tailored"
    assert store.get(job_id).tailored_resume  # non-empty


def test_api_tailor_gated_below_the_score_threshold_unless_forced(store, client):
    job_id = _seed(store, score=MIN_MATCH_SCORE_TO_TAILOR - 1)

    blocked = client.post(f"/api/jobs/{job_id}/tailor", json={})
    assert blocked.get_json()["ok"] is False
    assert store.get(job_id).status == "found"  # never touched

    forced = client.post(f"/api/jobs/{job_id}/tailor", json={"force": True})
    assert forced.get_json()["ok"] is True
    assert store.get(job_id).status == "tailored"


# --- apply (fill_application_form is mocked -- Playwright/network is out of scope
# here and already covered by tests/test_apply_tool.py) ---------------------------

def test_api_apply_returns_pending_fields_when_form_has_unmatched_fields(store, client, monkeypatch):
    job_id = _seed(store)
    store.update(job_id, application_method="web_form", application_target="http://x/apply")

    fake_result = ApplyResult(filled=[FilledField(label="Email", value="a@b.com", source="profile")],
                               unmatched_fields=["Why do you want this role?"],
                               screenshot_path="/tmp/shot.png")
    monkeypatch.setattr(app_module, "fill_application_form", lambda *a, **kw: fake_result)

    resp = client.post(f"/api/jobs/{job_id}/apply", json={"company": "Acme"})
    data = resp.get_json()
    assert data["ok"] is True
    assert data["kind"] == "needs_review"
    assert data["pending"]["labels"] == ["Why do you want this role?"]
    assert data["pending"]["filled"] == ["Email"]


def test_api_apply_resolve_saves_answers_and_marks_ready(store, client, monkeypatch):
    job_id = _seed(store)
    store.update(job_id, application_method="web_form", application_target="http://x/apply")
    fake_result = ApplyResult(filled=[], unmatched_fields=["Notice period"], screenshot_path="/tmp/shot.png")
    monkeypatch.setattr(app_module, "fill_application_form", lambda *a, **kw: fake_result)

    client.post(f"/api/jobs/{job_id}/apply", json={"company": "Acme"})
    resolve = client.post(f"/api/jobs/{job_id}/apply/resolve", json={"answers": ["30 days"]})
    data = resolve.get_json()

    assert data["ok"] is True
    assert data["status"] == "ready_to_submit"
    assert store.get(job_id).status == "ready_to_submit"


def test_api_apply_resolve_without_a_pending_review_is_rejected(store, client):
    job_id = _seed(store)
    resp = client.post(f"/api/jobs/{job_id}/apply/resolve", json={"answers": []})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_api_apply_drafts_an_email_for_email_application_method(store, client):
    job_id = _seed(store)
    store.update(job_id, application_method="email", application_target="jobs@acme.com")

    resp = client.post(f"/api/jobs/{job_id}/apply", json={"company": "Acme"})
    data = resp.get_json()
    assert data["ok"] is True
    assert data["kind"] == "email_drafted"
    assert data["status"] == "ready_to_submit"
    assert store.get(job_id).status == "ready_to_submit"


# --- discover (run_discovery itself is mocked -- its own behavior is covered by
# tests/test_discovery.py; this only checks app.py's background-thread wiring) -----

def _fake_run_discovery(client, resume_text, site_urls, store, **kwargs):
    from discovery import DiscoveryReport
    on_progress = kwargs.get("on_progress")
    if on_progress:
        on_progress({"done": 1, "total": 1, "label": "fake-source", "jobs_found": 3})
    return DiscoveryReport(jobs_found=3, sites_attempted=1, sites_failed=[])


def test_discover_runs_in_the_background_and_status_reflects_completion(client, monkeypatch):
    monkeypatch.setattr(app_module, "run_discovery", _fake_run_discovery)

    start = client.post("/discover", json={"query": "product manager"})
    assert start.get_json()["ok"] is True

    for _ in range(50):  # generous ceiling -- the fake worker finishes almost instantly
        status = client.get("/discover/status").get_json()
        if status["finished"]:
            break
        time.sleep(0.02)

    assert status["finished"] is True
    assert status["error"] is None
    assert status["summary"]["jobs_found"] == 3
    assert status["summary"]["sites_attempted"] == 1


def test_discover_refuses_a_second_run_while_one_is_in_progress(client, monkeypatch):
    monkeypatch.setattr(app_module, "_discover_state", {**app_module._discover_state, "running": True})

    resp = client.post("/discover", json={"query": "product manager"})
    data = resp.get_json()
    assert data["ok"] is False
    assert "already in progress" in data["info"]


def test_discover_surfaces_an_error_without_crashing_the_status_endpoint(client, monkeypatch):
    def _boom(*a, **kw):
        raise ConnectionError("[WinError 10053] An established connection was aborted")
    monkeypatch.setattr(app_module, "run_discovery", _boom)

    client.post("/discover", json={"query": "product manager"})
    for _ in range(50):
        status = client.get("/discover/status").get_json()
        if status["finished"]:
            break
        time.sleep(0.02)

    assert status["finished"] is True
    assert "WinError 10053" in status["error"]
