"""
Integration tests for the public demo gate (DEMO_MODE=1), exercised through
Flask's test client the same way test_app.py exercises the normal app. Every
test monkeypatches app.DEMO_MODE on rather than actually setting the env var
and reimporting, and points _job_store/_profile_store/_resume_path at temp
files so nothing here ever touches the real data/ files -- same isolation
principle as test_app.py's `store` fixture, just for the demo-mode surface
(the magic-link gate, the discover-run cap, apply being disabled) instead of
the normal single-user routes.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import app as app_module
import demo_gate
from job_store import JobStore as RealJobStore
from profile_store import ProfileStore as RealProfileStore
from tools.extract_tool import JobListing
from tools.score_tool import MatchResult


@pytest.fixture(autouse=True)
def reset_discover_state():
    app_module._discover_state.update(running=False, done=0, total=0, label="", jobs_found=0,
                                       error=None, finished=False, summary=None)
    yield
    app_module._discover_state.update(running=False, done=0, total=0, label="", jobs_found=0,
                                       error=None, finished=False, summary=None)


@pytest.fixture
def demo_client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DEMO_MODE", True)
    monkeypatch.setattr(app_module, "DEMO_ENGINE", "fake")
    monkeypatch.setattr(demo_gate, "DEFAULT_DB_PATH", tmp_path / "demo_access.db")

    real_store = RealJobStore(db_path=tmp_path / "demo_jobs.db")
    monkeypatch.setattr(app_module, "_job_store", lambda: real_store)

    profile_store = RealProfileStore(path=tmp_path / "demo_profile.json")
    monkeypatch.setattr(app_module, "_profile_store", lambda: profile_store)

    resume_path = tmp_path / "demo_resume.txt"
    resume_path.write_text("Jane Doe -- sample resume for tests.")
    monkeypatch.setattr(app_module, "_resume_path", lambda: resume_path)

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c, real_store


def _seed(store, title="Product Manager", score=95):
    listing = JobListing(title=title, location="Remote", url="http://x/job",
                          snippet="a real role", source_url="http://x")
    match = MatchResult(job=listing, score=score, rationale="strong fit",
                         matched_skills=["product"], missing_skills=["sql"])
    return store.add_match(match)


def _get_link(sent):
    """sent is the list a fake demo_gate sender appended (email, link) to."""
    return sent[-1][1]


def _verify(client, sent):
    token = _get_link(sent).rsplit("/", 1)[-1]
    return client.get(f"/demo/verify/{token}", follow_redirects=True)


# --- the gate itself ---------------------------------------------------------

def test_unverified_visitor_is_redirected_to_the_demo_landing_page(demo_client):
    client, _ = demo_client
    resp = client.get("/", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Try job_agent" in resp.data


def test_demo_landing_is_reachable_without_a_session(demo_client):
    client, _ = demo_client
    resp = client.get("/demo")
    assert resp.status_code == 200


def test_requesting_access_and_clicking_the_link_grants_a_session(demo_client, monkeypatch):
    client, _ = demo_client
    sent = []
    monkeypatch.setattr(demo_gate, "send_magic_link_email", lambda email, link: sent.append((email, link)))

    resp = client.post("/demo", data={"email": "visitor@example.com"}, follow_redirects=True)
    assert resp.status_code == 200
    assert len(sent) == 1

    resp = _verify(client, sent)
    assert resp.status_code == 200
    assert b"job_agent" in resp.data

    # now the dashboard itself is reachable
    resp = client.get("/api/jobs")
    assert resp.status_code == 200


def test_an_invalid_token_is_rejected_and_sent_back_to_the_landing_page(demo_client):
    client, _ = demo_client
    resp = client.get("/demo/verify/not-a-real-token", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Try job_agent" in resp.data


def test_a_used_token_cannot_grant_a_second_session(demo_client, monkeypatch):
    client, _ = demo_client
    sent = []
    monkeypatch.setattr(demo_gate, "send_magic_link_email", lambda email, link: sent.append((email, link)))
    client.post("/demo", data={"email": "onceonly@example.com"})
    token = _get_link(sent).rsplit("/", 1)[-1]

    first = client.get(f"/demo/verify/{token}")
    assert first.status_code == 302  # redirect to index on success

    with app_module.app.test_client() as fresh_client:  # a second, unrelated browser session
        second = fresh_client.get(f"/demo/verify/{token}", follow_redirects=True)
        assert b"Try job_agent" in second.data


# --- the discover-run cap -----------------------------------------------------

def test_discover_is_blocked_once_the_per_email_cap_is_used(demo_client, monkeypatch):
    client, store = demo_client
    monkeypatch.setattr(demo_gate, "MAX_RUNS_PER_EMAIL", 1)
    # The background worker itself (and its real network calls to Adzuna/RemoteOK)
    # is exercised elsewhere (test_discovery.py) -- here we only care whether the
    # /discover route's CAP CHECK, which runs synchronously before the thread ever
    # starts, lets the first request through and blocks the second.
    monkeypatch.setattr(app_module, "_discover_worker", lambda *a, **kw: None)
    sent = []
    monkeypatch.setattr(demo_gate, "send_magic_link_email", lambda email, link: sent.append((email, link)))
    client.post("/demo", data={"email": "runner@example.com"})
    _verify(client, sent)

    first = client.post("/discover", json={})
    assert first.get_json()["ok"] is True

    second = client.post("/discover", json={})
    body = second.get_json()
    assert body["ok"] is False
    assert "used your discover run" in body["info"].lower()


def test_discover_requires_a_verified_session(demo_client):
    # The before_request gate (test_unverified_visitor_is_redirected_... above)
    # already redirects any unauthenticated request before this route's own body
    # ever runs -- this just confirms /discover specifically is covered by it too,
    # not exempted by its own route registration.
    client, _ = demo_client
    resp = client.post("/discover", json={})
    assert resp.status_code == 302
    assert "/demo" in resp.headers["Location"]


# --- actions that are disabled or capped in demo mode -------------------------

def test_apply_is_disabled_in_demo_mode(demo_client, monkeypatch):
    client, store = demo_client
    sent = []
    monkeypatch.setattr(demo_gate, "send_magic_link_email", lambda email, link: sent.append((email, link)))
    client.post("/demo", data={"email": "applier@example.com"})
    _verify(client, sent)

    job_id = _seed(store)
    resp = client.post(f"/api/jobs/{job_id}/apply", json={"company": "Acme"})
    body = resp.get_json()
    assert body["ok"] is True
    assert body["kind"] == "demo_disabled"


def test_contacts_lookup_is_disabled_in_demo_mode(demo_client, monkeypatch):
    client, store = demo_client
    sent = []
    monkeypatch.setattr(demo_gate, "send_magic_link_email", lambda email, link: sent.append((email, link)))
    client.post("/demo", data={"email": "looker@example.com"})
    _verify(client, sent)

    job_id = _seed(store)
    resp = client.post(f"/api/jobs/{job_id}/contacts", json={"company": "Acme"})
    body = resp.get_json()
    assert body["contacts"] == []
    assert "disabled" in body["info"].lower()


def test_extra_actions_are_capped_across_tailor_outreach_prep(demo_client, monkeypatch):
    client, store = demo_client
    monkeypatch.setattr(demo_gate, "MAX_EXTRA_ACTIONS_PER_EMAIL", 1)
    sent = []
    monkeypatch.setattr(demo_gate, "send_magic_link_email", lambda email, link: sent.append((email, link)))
    client.post("/demo", data={"email": "clicker@example.com"})
    _verify(client, sent)

    job_id = _seed(store, score=95)
    first = client.post(f"/api/jobs/{job_id}/tailor", json={})
    assert first.status_code == 200  # allowed (fake engine, score above the gate)

    second = client.post(f"/api/jobs/{job_id}/prep", json={})
    body = second.get_json()
    assert body["ok"] is False
    assert "allowance" in body["error"].lower()


def test_add_lead_is_disabled_in_demo_mode(demo_client, monkeypatch):
    client, _ = demo_client
    sent = []
    monkeypatch.setattr(demo_gate, "send_magic_link_email", lambda email, link: sent.append((email, link)))
    client.post("/demo", data={"email": "leader@example.com"})
    _verify(client, sent)

    resp = client.post("/add-lead", data={"text": "some job posting"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"disabled in this public demo" in resp.data


def test_profile_edits_are_disabled_in_demo_mode(demo_client, monkeypatch):
    client, _ = demo_client
    sent = []
    monkeypatch.setattr(demo_gate, "send_magic_link_email", lambda email, link: sent.append((email, link)))
    client.post("/demo", data={"email": "editor@example.com"})
    _verify(client, sent)

    resp = client.post("/profile", data={"full_name": "Someone Else"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"disabled in this public demo" in resp.data
