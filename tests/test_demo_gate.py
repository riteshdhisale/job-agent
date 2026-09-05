"""Tests for the public demo's access gate (demo_gate.py) -- token issuance,
single-use verification, expiry, the per-email/per-IP rate limits, and the
per-email discover-run cap. No real SMTP anywhere: every test passes a fake
`_send` (or monkeypatches send_magic_link_email) instead of hitting Gmail."""
import time

import pytest

import demo_gate


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "demo_access.db"


def _fake_sender(sent):
    def send(email, link):
        sent.append((email, link))
    return send


def test_request_access_creates_a_working_link(db_path):
    sent = []
    result = demo_gate.request_access("visitor@example.com", "1.2.3.4", "http://localhost:5000",
                                       db_path=db_path, _send=_fake_sender(sent))
    assert result.ok
    assert len(sent) == 1
    email, link = sent[0]
    assert email == "visitor@example.com"
    assert "/demo/verify/" in link

    token = link.rsplit("/", 1)[-1]
    verified_email = demo_gate.verify_token(token, db_path=db_path)
    assert verified_email == "visitor@example.com"


def test_rejects_obviously_invalid_email(db_path):
    result = demo_gate.request_access("not-an-email", "1.2.3.4", "http://localhost:5000",
                                       db_path=db_path, _send=_fake_sender([]))
    assert not result.ok


def test_token_is_single_use(db_path):
    sent = []
    demo_gate.request_access("once@example.com", "1.2.3.4", "http://localhost:5000",
                              db_path=db_path, _send=_fake_sender(sent))
    token = sent[0][1].rsplit("/", 1)[-1]

    assert demo_gate.verify_token(token, db_path=db_path) == "once@example.com"
    # second click on the same link -- must not work again
    assert demo_gate.verify_token(token, db_path=db_path) is None


def test_unknown_token_fails(db_path):
    assert demo_gate.verify_token("not-a-real-token", db_path=db_path) is None


def test_expired_token_fails(db_path, monkeypatch):
    monkeypatch.setattr(demo_gate, "LINK_EXPIRY_MINUTES", 0)  # expires immediately
    sent = []
    demo_gate.request_access("slow@example.com", "1.2.3.4", "http://localhost:5000",
                              db_path=db_path, _send=_fake_sender(sent))
    token = sent[0][1].rsplit("/", 1)[-1]
    time.sleep(0.05)
    assert demo_gate.verify_token(token, db_path=db_path) is None


def test_per_ip_rate_limit(db_path, monkeypatch):
    monkeypatch.setattr(demo_gate, "MAX_REQUESTS_PER_IP_PER_HOUR", 2)
    sent = []
    for i in range(2):
        r = demo_gate.request_access(f"person{i}@example.com", "9.9.9.9", "http://localhost:5000",
                                      db_path=db_path, _send=_fake_sender(sent))
        assert r.ok
    blocked = demo_gate.request_access("person3@example.com", "9.9.9.9", "http://localhost:5000",
                                        db_path=db_path, _send=_fake_sender(sent))
    assert not blocked.ok
    assert "network" in blocked.message.lower()


def test_per_email_daily_rate_limit(db_path, monkeypatch):
    monkeypatch.setattr(demo_gate, "MAX_REQUESTS_PER_EMAIL_PER_DAY", 2)
    sent = []
    for i in range(2):
        r = demo_gate.request_access("repeat@example.com", f"1.1.1.{i}", "http://localhost:5000",
                                      db_path=db_path, _send=_fake_sender(sent))
        assert r.ok
    blocked = demo_gate.request_access("repeat@example.com", "1.1.1.9", "http://localhost:5000",
                                        db_path=db_path, _send=_fake_sender(sent))
    assert not blocked.ok


def test_a_send_failure_is_reported_cleanly_not_raised(db_path):
    def broken_sender(email, link):
        raise RuntimeError("smtp exploded")

    result = demo_gate.request_access("victim@example.com", "1.2.3.4", "http://localhost:5000",
                                       db_path=db_path, _send=broken_sender)
    assert not result.ok
    assert "smtp exploded" in result.message


def test_discover_run_cap(db_path, monkeypatch):
    monkeypatch.setattr(demo_gate, "MAX_RUNS_PER_EMAIL", 1)
    email = "runner@example.com"
    assert demo_gate.discover_runs_remaining(email, db_path=db_path) == 1
    demo_gate.record_discover_run(email, db_path=db_path)
    assert demo_gate.discover_runs_remaining(email, db_path=db_path) == 0
    # a second increment shouldn't go negative or error
    demo_gate.record_discover_run(email, db_path=db_path)
    assert demo_gate.discover_runs_remaining(email, db_path=db_path) == 0


def test_discover_run_cap_is_per_email_not_shared(db_path, monkeypatch):
    monkeypatch.setattr(demo_gate, "MAX_RUNS_PER_EMAIL", 1)
    demo_gate.record_discover_run("a@example.com", db_path=db_path)
    assert demo_gate.discover_runs_remaining("a@example.com", db_path=db_path) == 0
    assert demo_gate.discover_runs_remaining("b@example.com", db_path=db_path) == 1


def test_extra_action_cap(db_path, monkeypatch):
    monkeypatch.setattr(demo_gate, "MAX_EXTRA_ACTIONS_PER_EMAIL", 2)
    email = "clicker@example.com"
    assert demo_gate.extra_actions_remaining(email, db_path=db_path) == 2
    demo_gate.record_extra_action(email, db_path=db_path)
    assert demo_gate.extra_actions_remaining(email, db_path=db_path) == 1
    demo_gate.record_extra_action(email, db_path=db_path)
    assert demo_gate.extra_actions_remaining(email, db_path=db_path) == 0
    demo_gate.record_extra_action(email, db_path=db_path)
    assert demo_gate.extra_actions_remaining(email, db_path=db_path) == 0


def test_send_magic_link_email_requires_credentials(monkeypatch):
    monkeypatch.setattr(demo_gate, "GMAIL_ADDRESS", "")
    monkeypatch.setattr(demo_gate, "GMAIL_APP_PASSWORD", "")
    with pytest.raises(RuntimeError):
        demo_gate.send_magic_link_email("someone@example.com", "http://x/demo/verify/tok")
