"""Tests for the Adzuna source -- mocked HTTP throughout, no network or real
API keys needed. Covers the pagination behavior added so `discover` can find
"all 90%+ match jobs" instead of capping out at one page of ~20 results."""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.adzuna_source import fetch_adzuna_listings, MAX_RESULTS_PER_PAGE


def _page_response(n_jobs, status_code=200, **job_extra):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {
        "results": [
            {"title": f"Job {i}", "location": {"display_name": "Remote"},
             "redirect_url": f"http://x/{i}", "description": "<p>desc</p>", **job_extra}
            for i in range(n_jobs)
        ]
    }
    return resp


def test_missing_credentials_reports_and_does_not_call_network(monkeypatch):
    # A real .env with real Adzuna credentials (like the one on a machine actually
    # running this tool) gets loaded into os.environ by llm_client's load_dotenv()
    # as soon as any test module imports it during collection -- so this test has to
    # force the "no credentials" condition itself rather than trust the ambient
    # environment is empty.
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    with patch("tools.adzuna_source.requests.get") as mock_get:
        result = fetch_adzuna_listings("product manager", app_id=None, app_key=None)
    assert result.ok is False
    assert "ADZUNA_APP_ID" in result.note
    mock_get.assert_not_called()


def test_single_short_page_stops_after_one_request():
    """A page with fewer results than requested means we've hit the end --
    should not fetch a second page just to find it's empty."""
    with patch("tools.adzuna_source.requests.get", return_value=_page_response(7)) as mock_get:
        result = fetch_adzuna_listings("product manager", app_id="id", app_key="key", max_pages=5)
    assert result.ok is True
    assert len(result.listings) == 7
    assert result.pages_fetched == 1
    assert mock_get.call_count == 1


def test_full_pages_keep_paginating_until_max_pages_or_short_page():
    """Three full pages (== results_per_page) followed by a short page should
    fetch all four and stop -- not stop early, not loop forever."""
    full = MAX_RESULTS_PER_PAGE
    responses = [_page_response(full), _page_response(full), _page_response(full), _page_response(12)]
    with patch("tools.adzuna_source.requests.get", side_effect=responses) as mock_get:
        result = fetch_adzuna_listings("product manager", app_id="id", app_key="key", max_pages=10)
    assert result.pages_fetched == 4
    assert mock_get.call_count == 4
    assert len(result.listings) == full * 3 + 12


def test_max_pages_caps_requests_even_when_every_page_is_full():
    full = MAX_RESULTS_PER_PAGE
    with patch("tools.adzuna_source.requests.get", return_value=_page_response(full)) as mock_get:
        result = fetch_adzuna_listings("product manager", app_id="id", app_key="key", max_pages=3)
    assert result.pages_fetched == 3
    assert mock_get.call_count == 3
    assert len(result.listings) == full * 3


def test_error_on_first_page_reports_failure():
    with patch("tools.adzuna_source.requests.get", return_value=_page_response(0, status_code=403)):
        result = fetch_adzuna_listings("product manager", app_id="id", app_key="key")
    assert result.ok is False
    assert "403" in result.note


def test_error_on_later_page_keeps_the_results_already_fetched():
    """A hiccup on page 2 shouldn't throw away real results already pulled
    from page 1 -- surfacing partial coverage beats surfacing nothing."""
    responses = [_page_response(MAX_RESULTS_PER_PAGE), _page_response(0, status_code=500)]
    with patch("tools.adzuna_source.requests.get", side_effect=responses):
        result = fetch_adzuna_listings("product manager", app_id="id", app_key="key", max_pages=5)
    assert result.ok is True
    assert len(result.listings) == MAX_RESULTS_PER_PAGE
    assert result.pages_fetched == 1


def test_results_per_page_is_capped_at_adzuna_maximum():
    with patch("tools.adzuna_source.requests.get", return_value=_page_response(1)) as mock_get:
        fetch_adzuna_listings("product manager", app_id="id", app_key="key", results_per_page=999)
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["results_per_page"] == MAX_RESULTS_PER_PAGE


def test_posted_date_and_salary_are_captured_when_present():
    with patch("tools.adzuna_source.requests.get",
               return_value=_page_response(1, created="2026-09-01T10:00:00Z",
                                            salary_min=2500000, salary_max=3500000)):
        result = fetch_adzuna_listings("product manager", app_id="id", app_key="key")
    job = result.listings[0]
    assert job.posted_at == "2026-09-01T10:00:00Z"
    assert job.salary_min == 2500000
    assert job.salary_max == 3500000


def test_missing_posted_date_and_salary_stay_none_not_zero():
    with patch("tools.adzuna_source.requests.get", return_value=_page_response(1)):
        result = fetch_adzuna_listings("product manager", app_id="id", app_key="key")
    job = result.listings[0]
    assert job.posted_at is None
    assert job.salary_min is None
    assert job.salary_max is None


def test_min_salary_is_passed_through_as_a_real_query_param():
    with patch("tools.adzuna_source.requests.get", return_value=_page_response(1)) as mock_get:
        fetch_adzuna_listings("product manager", app_id="id", app_key="key", min_salary=3_000_000)
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["salary_min"] == 3_000_000


def test_min_salary_omitted_when_not_requested():
    with patch("tools.adzuna_source.requests.get", return_value=_page_response(1)) as mock_get:
        fetch_adzuna_listings("product manager", app_id="id", app_key="key")
    _, kwargs = mock_get.call_args
    assert "salary_min" not in kwargs["params"]


def test_max_days_old_is_passed_through_when_given():
    with patch("tools.adzuna_source.requests.get", return_value=_page_response(1)) as mock_get:
        fetch_adzuna_listings("product manager", app_id="id", app_key="key", max_days_old=3)
    _, kwargs = mock_get.call_args
    assert kwargs["params"]["max_days_old"] == 3
