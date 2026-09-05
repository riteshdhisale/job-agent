"""
Tool: freshness/salary/title/location filtering for discovered listings

Applies BEFORE scoring, on purpose: the whole point (per Ritesh's "use less
tokens" ask) is that a listing which fails these checks never reaches the
LLM at all, rather than getting scored and then discarded. Filtering here
costs nothing but a field/substring comparison; filtering via a scoring
prompt would cost tokens on every listing, pass or fail.

All filters are opt-in (None/empty = no filtering, existing behavior).
Missing data always passes ("unknown" is never treated as "fails") for the
freshness/salary/location checks -- most sources don't expose salary,
posting date, or even a location at all (only Adzuna reliably has
date+salary), and the decision made explicitly for this feature was not to
punish a listing for a gap in the source data.

Title keywords are the exception to "missing data passes": a title is
basically never missing, and this check exists specifically because
Greenhouse/Lever pull a company's *entire* job board (every department,
every level) with no query narrowing it down -- see tools/greenhouse_source.py
and tools/lever_source.py. Without a title filter, wiring in ~85 companies
means every Engineering/Sales/Design opening at every one of them gets
scored against the resume too, which is both the literal cause of the "these
roles are not matching" gap Ritesh flagged and, once real company boards are
involved, a genuine cost/time problem for the `claude`/`gemini` engines (added
2026-09-04, alongside data/greenhouse_boards.json + data/lever_sites.json).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Sequence

from tools.extract_tool import JobListing


def _parse_posted_at(posted_at: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None  # unparseable date is treated the same as no date: doesn't fail the listing


def _passes_freshness(job: JobListing, max_age_hours: Optional[int], now: datetime) -> bool:
    if max_age_hours is None or not job.posted_at:
        return True
    posted = _parse_posted_at(job.posted_at)
    if posted is None:
        return True
    age_hours = (now - posted).total_seconds() / 3600
    return 0 <= age_hours <= max_age_hours


def _passes_salary(job: JobListing, min_salary: Optional[int]) -> bool:
    if min_salary is None:
        return True
    # Prefer the top of a disclosed range (a 25-35L posting genuinely can pay 30L+);
    # fall back to salary_min if that's the only figure given.
    effective = job.salary_max if job.salary_max is not None else job.salary_min
    if effective is None:
        return True  # not disclosed -- included per the "don't punish missing data" decision
    return effective >= min_salary


def _passes_title_keywords(job: JobListing, include_keywords: Optional[Sequence[str]],
                            exclude_keywords: Optional[Sequence[str]]) -> bool:
    title = (job.title or "").lower()
    if exclude_keywords and any(kw.lower() in title for kw in exclude_keywords if kw):
        return False
    if include_keywords and not any(kw.lower() in title for kw in include_keywords if kw):
        return False
    return True


def _passes_location(job: JobListing, allowed_locations: Optional[Sequence[str]]) -> bool:
    if not allowed_locations:
        return True
    location = (job.location or "").strip()
    if not location:
        return True  # undisclosed location -- don't punish missing data, same principle as salary/date
    location_lower = location.lower()
    return any(loc.lower() in location_lower for loc in allowed_locations if loc)


def filter_listings(jobs: List[JobListing], max_age_hours: Optional[int] = None,
                     min_salary: Optional[int] = None, include_keywords: Optional[Sequence[str]] = None,
                     exclude_keywords: Optional[Sequence[str]] = None,
                     allowed_locations: Optional[Sequence[str]] = None,
                     now: Optional[datetime] = None) -> List[JobListing]:
    """Returns only the listings from `jobs` that pass every active filter. Called once
    per source, right after fetch and before score_job_matches, so filtered-out listings
    never cost an LLM call. `now` is injectable for tests; defaults to the real time.

    `include_keywords`: a listing's title must contain at least one (case-insensitive
    substring match) to survive -- e.g. ["Product Manager", "TPM", "Business Analyst"].
    `exclude_keywords`: a listing whose title contains any of these is dropped even if it
    matched `include_keywords` -- e.g. ["Intern", "VP"]. `allowed_locations`: a listing
    with a disclosed location matching none of these is dropped; a blank/unknown location
    always passes (see module docstring)."""
    if (max_age_hours is None and min_salary is None and not include_keywords
            and not exclude_keywords and not allowed_locations):
        return jobs
    now = now or datetime.now(timezone.utc)
    return [j for j in jobs if _passes_freshness(j, max_age_hours, now)
            and _passes_salary(j, min_salary)
            and _passes_title_keywords(j, include_keywords, exclude_keywords)
            and _passes_location(j, allowed_locations)]
