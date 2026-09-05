"""Tests for the Agent 1 discovery pipeline against local fixtures --
no network needed. This is the same fixture-based approach the original
Agent 1 build used, adapted to check jobs land correctly in the JobStore."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discovery
from llm_client import FakeLLMClient
from job_store import JobStore
from discovery import run_discovery, _normalize_queries, _dedupe_by_url
from tools.extract_tool import JobListing
from tools.greenhouse_source import GreenhouseSourceResult
from tools.lever_source import LeverSourceResult

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "data" / "sample_pages"
RESUME_PATH = Path(__file__).resolve().parent.parent / "data" / "resume.txt"

SITE_URLS = [
    str(FIXTURES_DIR / "example_careers_northwind.html"),
    str(FIXTURES_DIR / "example_careers_lighthouse.html"),
]


def _run(tmp_path, **kwargs):
    store = JobStore(db_path=tmp_path / "jobs.db")
    resume_text = RESUME_PATH.read_text()
    report = run_discovery(FakeLLMClient(), resume_text, SITE_URLS, store,
                            use_adzuna=False, use_remoteok=False, **kwargs)
    return report, store


def test_discovery_persists_jobs_into_the_store(tmp_path):
    report, store = _run(tmp_path)
    assert report.sites_attempted == 2
    assert report.sites_failed == []
    jobs = store.list()
    assert len(jobs) == report.jobs_found
    assert len(jobs) >= 4  # both fixture sites have multiple listings


def test_discovered_jobs_default_to_career_site_web_form(tmp_path):
    _, store = _run(tmp_path)
    for j in store.list():
        assert j.source_type == "career_site"
        assert j.application_method == "web_form"
        assert j.status == "found"


def test_disabling_api_sources_reports_no_failures_for_them(tmp_path):
    report, _ = _run(tmp_path)
    assert not any("adzuna" in f for f in report.sites_failed)
    assert not any("remoteok" in f for f in report.sites_failed)


def test_missing_site_fails_gracefully_without_crashing(tmp_path):
    store = JobStore(db_path=tmp_path / "jobs.db")
    resume_text = RESUME_PATH.read_text()
    report = run_discovery(FakeLLMClient(), resume_text, ["data/sample_pages/does_not_exist.html"],
                            store, use_adzuna=False, use_remoteok=False)
    assert report.sites_attempted == 1
    assert len(report.sites_failed) == 1


def test_on_progress_fires_once_per_source_with_running_totals(tmp_path):
    """The web app's progress bar depends on this firing exactly once per source
    attempted (each career site, plus Adzuna/RemoteOK/Greenhouse/Lever if enabled),
    with a monotonically increasing `done` count and a `jobs_found` running total that
    matches the final report -- verified here without needing a real Flask request."""
    events = []
    report, store = _run(tmp_path, on_progress=lambda evt: events.append(dict(evt)))

    assert len(events) == report.sites_attempted  # one tick per career site, Adzuna/RemoteOK disabled
    assert [e["done"] for e in events] == list(range(1, len(events) + 1))
    assert all(e["total"] == report.sites_attempted for e in events)
    # jobs_found should never decrease, and should end at the true total
    jobs_found_seq = [e["jobs_found"] for e in events]
    assert jobs_found_seq == sorted(jobs_found_seq)
    assert jobs_found_seq[-1] == report.jobs_found
    # each event names which source it just finished
    assert events[0]["label"] == SITE_URLS[0]
    assert events[1]["label"] == SITE_URLS[1]


def test_normalize_queries_splits_a_comma_separated_string():
    # This is what the CLI's --query and the web app's query field both pass through
    # -- Ritesh's resume covers PM/Product Owner/Business Analyst roles, and the
    # default search now covers all three in one run rather than just "product manager".
    assert _normalize_queries("product manager, product owner, business analyst") == [
        "product manager", "product owner", "business analyst",
    ]


def test_normalize_queries_accepts_a_plain_list_too():
    assert _normalize_queries(["product manager", "product owner"]) == ["product manager", "product owner"]


def test_normalize_queries_a_single_string_with_no_comma_is_a_one_item_list():
    assert _normalize_queries("product manager") == ["product manager"]


def test_normalize_queries_strips_whitespace_and_drops_empties():
    assert _normalize_queries("product manager,  , product owner ,") == ["product manager", "product owner"]


def test_dedupe_by_url_keeps_first_occurrence_only():
    a = JobListing(title="A", location="Remote", url="http://x/1", snippet="", source_url="adzuna:in:product manager")
    b = JobListing(title="A (dup)", location="Remote", url="http://x/1", snippet="", source_url="adzuna:in:product owner")
    c = JobListing(title="C", location="Remote", url="http://x/2", snippet="", source_url="adzuna:in:product manager")
    assert _dedupe_by_url([a, b, c]) == [a, c]


def test_dedupe_by_url_falls_back_to_title_and_source_when_url_is_blank():
    # Some sources occasionally hand back a listing with no url at all -- shouldn't
    # collapse every url-less listing into one via a shared empty-string key.
    a = JobListing(title="A", location="Remote", url="", snippet="", source_url="adzuna:in:product manager")
    b = JobListing(title="B", location="Remote", url="", snippet="", source_url="adzuna:in:product manager")
    assert _dedupe_by_url([a, b]) == [a, b]


# --- Greenhouse/Lever wiring (mocked HTTP -- the sources' own HTTP behavior is covered
# in tests/test_new_sources.py; this only checks run_discovery calls them and persists
# what they return, including the title/location pre-filter that matters most here since
# these two sources hand back a company's *entire* board with no query of their own) -----

def test_greenhouse_and_lever_boards_are_fetched_and_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "fetch_greenhouse_listings", lambda token, **kw: GreenhouseSourceResult(
        ok=True, listings=[JobListing(title="Product Manager", location="Bengaluru", url="http://gh/1",
                                       snippet="", source_url=f"greenhouse:{token}")]))
    monkeypatch.setattr(discovery, "fetch_lever_listings", lambda site, **kw: LeverSourceResult(
        ok=True, listings=[JobListing(title="Product Owner", location="Remote - India", url="http://lv/1",
                                       snippet="", source_url=f"lever:{site}")]))

    store = JobStore(db_path=tmp_path / "jobs.db")
    report = run_discovery(FakeLLMClient(), RESUME_PATH.read_text(), [], store, use_adzuna=False,
                            use_remoteok=False, greenhouse_boards=["acme"], lever_sites=["acme"])

    assert report.jobs_found == 2
    titles = sorted(j.title for j in store.list())
    assert titles == ["Product Manager", "Product Owner"]
    assert all(j.source_type == "job_portal_api" for j in store.list())


def test_greenhouse_board_failure_is_reported_not_crashed(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "fetch_greenhouse_listings", lambda token, **kw: GreenhouseSourceResult(
        ok=False, listings=[], note=f"no board found for token '{token}'"))

    store = JobStore(db_path=tmp_path / "jobs.db")
    report = run_discovery(FakeLLMClient(), RESUME_PATH.read_text(), [], store, use_adzuna=False,
                            use_remoteok=False, greenhouse_boards=["not-a-real-token"])

    assert report.jobs_found == 0
    assert any("not-a-real-token" in f for f in report.sites_failed)


def test_title_keywords_filter_out_unrelated_roles_from_a_greenhouse_board(tmp_path, monkeypatch):
    # This is the exact "these roles are not matching" gap: a board token returns every
    # open role at the company, across every department, unless title_keywords narrows it.
    monkeypatch.setattr(discovery, "fetch_greenhouse_listings", lambda token, **kw: GreenhouseSourceResult(
        ok=True, listings=[
            JobListing(title="Backend Engineer", location="Bengaluru", url="http://gh/1", snippet="",
                       source_url=f"greenhouse:{token}"),
            JobListing(title="Senior Product Manager", location="Bengaluru", url="http://gh/2", snippet="",
                       source_url=f"greenhouse:{token}"),
        ]))

    store = JobStore(db_path=tmp_path / "jobs.db")
    report = run_discovery(FakeLLMClient(), RESUME_PATH.read_text(), [], store, use_adzuna=False,
                            use_remoteok=False, greenhouse_boards=["acme"],
                            title_keywords=["Product Manager"])

    assert report.jobs_found == 1
    assert store.list()[0].title == "Senior Product Manager"


def test_allowed_locations_filters_out_of_region_roles_from_a_lever_site(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "fetch_lever_listings", lambda site, **kw: LeverSourceResult(
        ok=True, listings=[
            JobListing(title="Product Manager", location="San Francisco, CA", url="http://lv/1", snippet="",
                       source_url=f"lever:{site}"),
            JobListing(title="Product Manager", location="Bengaluru, India", url="http://lv/2", snippet="",
                       source_url=f"lever:{site}"),
        ]))

    store = JobStore(db_path=tmp_path / "jobs.db")
    report = run_discovery(FakeLLMClient(), RESUME_PATH.read_text(), [], store, use_adzuna=False,
                            use_remoteok=False, lever_sites=["acme"],
                            allowed_locations=["Bengaluru"])

    assert report.jobs_found == 1
    assert store.list()[0].location == "Bengaluru, India"
