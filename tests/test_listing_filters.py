"""Tests for tools/listing_filters.py -- the pre-scoring freshness/salary
filter built so a listing that fails either check never costs an LLM call.
Pure functions, no mocking needed beyond an injectable `now`."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.extract_tool import JobListing
from tools.listing_filters import filter_listings

NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)


def _job(**kwargs):
    defaults = dict(title="Product Manager", location="Remote", url="http://x/1",
                     snippet="desc", source_url="adzuna:in:product manager")
    defaults.update(kwargs)
    return JobListing(**defaults)


def test_no_filters_passes_everything_unchanged():
    jobs = [_job(), _job(url="http://x/2", posted_at=None, salary_min=None)]
    assert filter_listings(jobs) == jobs


def test_missing_posted_at_always_passes_freshness_filter():
    job = _job(posted_at=None)
    assert filter_listings([job], max_age_hours=48, now=NOW) == [job]


def test_within_window_passes():
    job = _job(posted_at=(NOW - timedelta(hours=10)).isoformat())
    assert filter_listings([job], max_age_hours=48, now=NOW) == [job]


def test_outside_window_is_excluded():
    job = _job(posted_at=(NOW - timedelta(hours=72)).isoformat())
    assert filter_listings([job], max_age_hours=48, now=NOW) == []


def test_exactly_at_the_boundary_passes():
    job = _job(posted_at=(NOW - timedelta(hours=48)).isoformat())
    assert filter_listings([job], max_age_hours=48, now=NOW) == [job]


def test_unparseable_date_passes_rather_than_excludes():
    job = _job(posted_at="not-a-real-date")
    assert filter_listings([job], max_age_hours=48, now=NOW) == [job]


def test_missing_salary_always_passes_salary_filter():
    job = _job(salary_min=None, salary_max=None)
    assert filter_listings([job], min_salary=3_000_000) == [job]


def test_salary_meeting_the_floor_passes():
    job = _job(salary_min=3_500_000, salary_max=4_000_000)
    assert filter_listings([job], min_salary=3_000_000) == [job]


def test_salary_below_the_floor_is_excluded():
    job = _job(salary_min=1_500_000, salary_max=2_000_000)
    assert filter_listings([job], min_salary=3_000_000) == []


def test_a_range_straddling_the_floor_passes_on_its_upper_end():
    # 25L-35L: the top of the range clears 30L even though the bottom doesn't --
    # a real posting like this genuinely can pay 30L+, so it should show up.
    job = _job(salary_min=2_500_000, salary_max=3_500_000)
    assert filter_listings([job], min_salary=3_000_000) == [job]


def test_only_salary_min_given_is_used_when_max_is_absent():
    job = _job(salary_min=2_000_000, salary_max=None)
    assert filter_listings([job], min_salary=3_000_000) == []
    job2 = _job(url="http://x/2", salary_min=3_500_000, salary_max=None)
    assert filter_listings([job2], min_salary=3_000_000) == [job2]


def test_both_filters_applied_together():
    fresh_and_well_paid = _job(url="http://x/1", posted_at=NOW.isoformat(), salary_min=4_000_000)
    fresh_but_underpaid = _job(url="http://x/2", posted_at=NOW.isoformat(), salary_min=1_000_000)
    stale_but_well_paid = _job(url="http://x/3", posted_at=(NOW - timedelta(days=10)).isoformat(),
                                salary_min=4_000_000)
    result = filter_listings([fresh_and_well_paid, fresh_but_underpaid, stale_but_well_paid],
                              max_age_hours=48, min_salary=3_000_000, now=NOW)
    assert result == [fresh_and_well_paid]


# --- title keyword filter -- the actual fix for Greenhouse/Lever handing back a whole
# company board (every department, every level) with no query of their own -----------

def test_no_keyword_filters_passes_everything_unchanged():
    jobs = [_job(title="Backend Engineer"), _job(url="http://x/2", title="Product Manager")]
    assert filter_listings(jobs) == jobs


def test_include_keywords_keeps_only_matching_titles():
    pm = _job(url="http://x/1", title="Senior Product Manager")
    eng = _job(url="http://x/2", title="Backend Engineer")
    result = filter_listings([pm, eng], include_keywords=["Product Manager", "TPM"])
    assert result == [pm]


def test_include_keywords_is_case_insensitive():
    job = _job(title="product manager, growth")
    assert filter_listings([job], include_keywords=["Product Manager"]) == [job]


def test_exclude_keywords_drops_matching_titles_even_if_included():
    intern = _job(title="Product Manager Intern")
    real = _job(url="http://x/2", title="Product Manager")
    result = filter_listings([intern, real], include_keywords=["Product Manager"],
                              exclude_keywords=["Intern"])
    assert result == [real]


def test_exclude_keywords_alone_works_without_include_keywords():
    vp = _job(title="VP, Product")
    ic = _job(url="http://x/2", title="Product Manager")
    result = filter_listings([vp, ic], exclude_keywords=["VP"])
    assert result == [ic]


# --- location filter ------------------------------------------------------------------

def test_missing_location_always_passes_location_filter():
    job = _job(location="")
    assert filter_listings([job], allowed_locations=["Bengaluru", "Pune"]) == [job]


def test_matching_location_passes():
    job = _job(location="Bengaluru, Karnataka")
    assert filter_listings([job], allowed_locations=["Bengaluru", "Pune"]) == [job]


def test_non_matching_disclosed_location_is_excluded():
    job = _job(location="San Francisco, CA")
    assert filter_listings([job], allowed_locations=["Bengaluru", "Pune"]) == []


def test_remote_india_style_location_matches_its_own_allowed_entry():
    job = _job(location="Remote - India")
    assert filter_listings([job], allowed_locations=["Remote - India", "Bengaluru"]) == [job]


def test_title_and_location_filters_combine_with_freshness_and_salary():
    good = _job(url="http://x/1", title="Product Manager", location="Bengaluru",
                posted_at=NOW.isoformat(), salary_min=4_000_000)
    wrong_title = _job(url="http://x/2", title="Backend Engineer", location="Bengaluru",
                        posted_at=NOW.isoformat(), salary_min=4_000_000)
    wrong_location = _job(url="http://x/3", title="Product Manager", location="San Francisco",
                           posted_at=NOW.isoformat(), salary_min=4_000_000)
    result = filter_listings([good, wrong_title, wrong_location], max_age_hours=48, min_salary=3_000_000,
                              include_keywords=["Product Manager"], allowed_locations=["Bengaluru"], now=NOW)
    assert result == [good]
