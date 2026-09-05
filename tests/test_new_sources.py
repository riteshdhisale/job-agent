"""
Tests for the compliant-API sources (Greenhouse, Lever) and the rate
limiter, using mocked HTTP responses -- deterministic, no real network
needed, and won't behave differently depending on whether the machine
running them can actually reach these APIs.
"""
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from tools.greenhouse_source import fetch_greenhouse_listings
from tools.lever_source import fetch_lever_listings
from tools.rate_limit import check_and_record, CooldownActive


def _fake_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    return resp


@patch("tools.greenhouse_source.requests.get")
def test_greenhouse_maps_fields_correctly(mock_get):
    mock_get.return_value = _fake_response(json_data={
        "jobs": [{
            "title": "Senior Product Manager", "location": {"name": "Remote"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
            "content": "<p>Own the <b>roadmap</b> for our platform.</p>",
        }]
    })
    result = fetch_greenhouse_listings("acme")
    assert result.ok
    assert len(result.listings) == 1
    job = result.listings[0]
    assert job.title == "Senior Product Manager"
    assert job.location == "Remote"
    assert "roadmap" in job.snippet
    assert "<b>" not in job.snippet  # HTML stripped


@patch("tools.greenhouse_source.requests.get")
def test_greenhouse_unescapes_html_entities_before_stripping_tags(mock_get):
    # Real live bug, 2026-09-05: some Greenhouse boards hand back content that is
    # HTML *entity-escaped* inside the JSON string -- literal "&lt;div&gt;" text, not
    # an actual "<div>" tag -- so the old _strip_html (a bare "<[^>]+>" regex) never
    # matched anything and the whole 500-char snippet budget got eaten by escaped
    # markup instead of real job-description text. Confirmed against Ritesh's real
    # jobs.db: 29/29 stored Greenhouse snippets were 100% escaped markup.
    mock_get.return_value = _fake_response(json_data={
        "jobs": [{
            "title": "Senior Product Manager", "location": {"name": "Remote"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
            "content": "&lt;div class=&quot;content-intro&quot;&gt;&lt;p&gt;Own the &lt;b&gt;roadmap&lt;/b&gt; for our platform.&lt;/p&gt;&lt;/div&gt;",
        }]
    })
    result = fetch_greenhouse_listings("acme")
    assert result.ok
    job = result.listings[0]
    assert "roadmap" in job.snippet
    assert "&lt;" not in job.snippet and "&gt;" not in job.snippet  # entities decoded
    assert "<" not in job.snippet and ">" not in job.snippet  # then tags stripped


@patch("tools.greenhouse_source.requests.get")
def test_greenhouse_404_is_reported_not_crashed(mock_get):
    mock_get.return_value = _fake_response(status_code=404)
    result = fetch_greenhouse_listings("does-not-exist")
    assert not result.ok
    assert "does-not-exist" in result.note


@patch("tools.lever_source.requests.get")
def test_lever_maps_fields_correctly(mock_get):
    mock_get.return_value = _fake_response(json_data=[{
        "text": "Product Manager, Growth",
        "categories": {"location": "Bengaluru, India"},
        "hostedUrl": "https://jobs.lever.co/acme/abc",
        "descriptionPlain": "Drive growth initiatives across the funnel.",
    }])
    result = fetch_lever_listings("acme")
    assert result.ok
    assert result.listings[0].title == "Product Manager, Growth"
    assert result.listings[0].location == "Bengaluru, India"


@patch("tools.lever_source.requests.get")
def test_lever_unexpected_shape_is_reported_not_crashed(mock_get):
    mock_get.return_value = _fake_response(json_data={"unexpected": "shape"})
    result = fetch_lever_listings("acme")
    assert not result.ok


def test_rate_limit_allows_first_run_then_blocks(tmp_path):
    state_path = tmp_path / "cooldowns.json"
    check_and_record("test_source", cooldown_seconds=3600, state_path=state_path)  # first run: fine

    with pytest.raises(CooldownActive):
        check_and_record("test_source", cooldown_seconds=3600, state_path=state_path)  # second run: blocked


def test_rate_limit_force_overrides_cooldown(tmp_path):
    state_path = tmp_path / "cooldowns.json"
    check_and_record("test_source", cooldown_seconds=3600, state_path=state_path)
    check_and_record("test_source", cooldown_seconds=3600, state_path=state_path, force=True)  # should not raise


def test_rate_limit_allows_after_cooldown_elapses(tmp_path):
    state_path = tmp_path / "cooldowns.json"
    # seed the state as if the source ran long ago
    state_path.write_text(json.dumps({"test_source": time.time() - 999999}))
    check_and_record("test_source", cooldown_seconds=3600, state_path=state_path)  # should not raise


def test_rate_limit_tracks_sources_independently(tmp_path):
    state_path = tmp_path / "cooldowns.json"
    check_and_record("source_a", cooldown_seconds=3600, state_path=state_path)
    check_and_record("source_b", cooldown_seconds=3600, state_path=state_path)  # different source -- fine
    with pytest.raises(CooldownActive):
        check_and_record("source_a", cooldown_seconds=3600, state_path=state_path)
