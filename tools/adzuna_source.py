"""
Source: Adzuna aggregator API

Adzuna aggregates listings from many job boards (including a lot of what
you'd otherwise find manually on portal sites) behind one public, ToS-
compliant API -- no scraping, no login wall, real terms of service that
allow this use. Confirmed to cover India (country code "in") along with
11 other countries as of this research; see 02_business_strategy.md-style
sourcing notes in the README.

Free tier: ~1,000 calls/month. Sign up at https://developer.adzuna.com to
get an app_id/app_key pair (no cost) -- set ADZUNA_APP_ID / ADZUNA_APP_KEY.

Unlike career-site scanning, Adzuna already returns structured data, so no
LLM extraction step is needed here -- this source hands JobListing objects
straight to score_job_matches.

Paginates by design: Ritesh's explicit ask was "find all 90%+ match jobs at
any given time, execution time does not matter" -- one page of ~20 results
would silently cap coverage well below what Adzuna actually has for a broad
query like "product manager". `max_pages` (default 5, page size capped at
Adzuna's own maximum of 50) trades a handful of extra API calls -- still
nowhere near the ~1,000/month free-tier ceiling for a tool one person runs a
few times a day -- for up to 250 listings per run instead of 20. It stops
early the moment a page comes back short or empty, so a query with fewer
real results doesn't burn calls chasing pages that aren't there.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional

import requests

from tools.extract_tool import JobListing

BASE_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
MAX_RESULTS_PER_PAGE = 50  # Adzuna's own documented ceiling per page


@dataclass
class AdzunaSourceResult:
    ok: bool
    listings: List[JobListing]
    note: str = ""
    pages_fetched: int = 0


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def _listing_from_job(job: dict, country: str, query: str) -> JobListing:
    return JobListing(
        title=job.get("title", "").strip(),
        location=(job.get("location", {}) or {}).get("display_name", ""),
        url=job.get("redirect_url", ""),
        snippet=_strip_html(job.get("description", ""))[:500],
        source_url=f"adzuna:{country}:{query}",
        # Adzuna returns these on every listing already -- previously just not captured.
        # `created` is an ISO-ish timestamp string; salary_min/max are ints in the
        # country's local currency (INR for "in"), or absent entirely when a posting
        # doesn't disclose pay -- see tools/listing_filters.py for how "not disclosed"
        # is handled (never treated as failing a salary filter).
        posted_at=job.get("created") or None,
        salary_min=int(job["salary_min"]) if job.get("salary_min") else None,
        salary_max=int(job["salary_max"]) if job.get("salary_max") else None,
    )


def fetch_adzuna_listings(query: str, country: str = "in", results_per_page: int = MAX_RESULTS_PER_PAGE,
                           max_pages: int = 5, app_id: Optional[str] = None, app_key: Optional[str] = None,
                           min_salary: Optional[int] = None, max_days_old: Optional[int] = None) -> AdzunaSourceResult:
    aid = app_id or os.environ.get("ADZUNA_APP_ID")
    akey = app_key or os.environ.get("ADZUNA_APP_KEY")
    if not aid or not akey:
        return AdzunaSourceResult(ok=False, listings=[], note="ADZUNA_APP_ID/ADZUNA_APP_KEY not set -- skipping")

    results_per_page = min(results_per_page, MAX_RESULTS_PER_PAGE)
    listings: List[JobListing] = []
    pages_fetched = 0

    params = {"app_id": aid, "app_key": akey, "results_per_page": results_per_page,
              "what": query, "content-type": "application/json"}
    if min_salary is not None:
        params["salary_min"] = min_salary  # a real, documented Adzuna param -- server-side filtering, free
    if max_days_old is not None:
        # Not confirmed as a stable documented parameter for every Adzuna account/plan --
        # harmless to send even if Adzuna ignores it (unknown params are typically
        # dropped, not rejected), and tools/listing_filters.py re-checks `created`
        # client-side regardless, so correctness never depends on this actually working.
        params["max_days_old"] = max_days_old

    for page in range(1, max(max_pages, 1) + 1):
        try:
            resp = requests.get(
                BASE_URL.format(country=country, page=page),
                params=params,
                timeout=15,
            )
        except requests.RequestException as exc:
            if listings:
                # we already got real results from earlier pages -- report those rather
                # than throwing them away because a later page hiccuped
                break
            return AdzunaSourceResult(ok=False, listings=[], note=f"network error: {exc}")

        if resp.status_code != 200:
            if listings:
                break
            return AdzunaSourceResult(ok=False, listings=[], note=f"HTTP {resp.status_code}: {resp.text[:200]}")

        pages_fetched += 1
        page_jobs = resp.json().get("results", [])
        listings.extend(_listing_from_job(job, country, query) for job in page_jobs)

        if len(page_jobs) < results_per_page:
            break  # short page -- this was the last one, no point requesting further

    return AdzunaSourceResult(ok=True, listings=listings, pages_fetched=pages_fetched)
