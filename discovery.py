"""
Agent 1: Discovery -- fetch -> extract -> score -> persist, across every
compliant source this system knows about. Same deterministic pipeline
design as the original Agent 1 build (see ../01_agentic_ai_primer.md,
section 4), adapted to write every match into the JobStore so Agents 2-5
can act on results across separate CLI runs.

Sources, in order:
  1. Direct career-site scanning (fetch + LLM extraction)
  2. Adzuna (aggregator API, compliant)
  3. RemoteOK (open feed, compliant)
  4. Greenhouse (official per-company API, compliant)
  5. Lever (official per-company API, compliant)

NOT included here (by design, see cli.py and the README's sources table):
Instahyre (scraping -- opt-in only, via `cli.py discover --include-instahyre`,
rate-limited), Naukri (see tools/naukri_note.md for why), and LinkedIn posts /
portal email alerts / agency messages / live-browsed pages (all go through
`cli.py add-lead` or `add-browsed-page` instead, since they're pasted/
captured text, not something this function fetches on its own).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Union

from llm_client import LLMClient
from job_store import JobStore
from tools.fetch_tool import fetch_career_page
from tools.extract_tool import extract_job_listings
from tools.score_tool import score_job_matches
from tools.adzuna_source import fetch_adzuna_listings
from tools.remoteok_source import fetch_remoteok_listings
from tools.greenhouse_source import fetch_greenhouse_listings
from tools.lever_source import fetch_lever_listings
from tools.listing_filters import filter_listings

MAX_SITES_PER_RUN = 25  # hard stop -- see the primer's exercise 2
DEFAULT_QUERY = "product manager, product owner, business analyst"


@dataclass
class DiscoveryReport:
    jobs_found: int
    sites_attempted: int
    sites_failed: List[str] = field(default_factory=list)


def _normalize_queries(query: Union[str, Sequence[str]]) -> List[str]:
    """A single role search is still just a 1-item list internally. A comma-separated
    string (from the CLI's --query or the web app's query field) or an actual list both
    work -- e.g. Ritesh's resume covers Product Manager, Product Owner, and Business
    Analyst roles, and he wants all three searched against the same profile in one run."""
    if isinstance(query, str):
        return [q.strip() for q in query.split(",") if q.strip()]
    return [q.strip() for q in query if q and q.strip()]


def _dedupe_by_url(listings):
    seen = set()
    deduped = []
    for job in listings:
        key = job.url or (job.title, job.source_url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(job)
    return deduped


def run_discovery(client: LLMClient, resume_text: str, site_urls: List[str], job_store: JobStore,
                   query: Union[str, Sequence[str]] = DEFAULT_QUERY, country: str = "in",
                   use_adzuna: bool = True, use_remoteok: bool = True,
                   greenhouse_boards: List[str] = (), lever_sites: List[str] = (),
                   max_age_hours: Optional[int] = None, min_salary: Optional[int] = None,
                   title_keywords: Sequence[str] = (), exclude_title_keywords: Sequence[str] = (),
                   allowed_locations: Sequence[str] = (),
                   on_progress: Optional[Callable[[dict], None]] = None) -> DiscoveryReport:
    """`on_progress`, if given, is called after each source is attempted (one career
    site, or one of Adzuna/RemoteOK, or one Greenhouse board, or one Lever site) with
    {"done": int, "total": int, "label": str, "jobs_found": int} -- everything a caller
    needs to render a progress bar. Purely additive: existing callers that don't pass it
    (the CLI, every test) behave exactly as before.

    `max_age_hours`/`min_salary` (both None by default = no filtering, unchanged
    behavior) are applied via tools/listing_filters.py right after each source is
    fetched and BEFORE scoring -- a listing that fails either check never costs an LLM
    call. Only Adzuna currently reports a real posting date/salary; everything else's
    listings simply don't have that data (None), which always passes rather than fails
    (see listing_filters.py's docstring) -- so this never silently empties out your
    other sources, it only tightens the one that actually has the data to tighten on.

    `title_keywords`/`exclude_title_keywords`/`allowed_locations` (all empty by default =
    no filtering) are the same kind of pre-scoring filter, applied to every source
    including Greenhouse/Lever -- which, unlike Adzuna/RemoteOK, have no query to narrow
    them down at all; fetching a company's board means every open role there, every
    department. Without a title filter this is also what was silently costing LLM calls
    on completely unrelated roles (see the "these roles are not matching" gap)."""
    site_urls = site_urls[:MAX_SITES_PER_RUN]
    queries = _normalize_queries(query)
    failed: List[str] = []
    jobs_found = 0

    total = len(site_urls) + int(use_adzuna) + int(use_remoteok) + len(greenhouse_boards) + len(lever_sites)
    done = 0

    def _tick(label: str) -> None:
        nonlocal done
        done += 1
        if on_progress:
            on_progress({"done": done, "total": total, "label": label, "jobs_found": jobs_found})

    def _filter(listings):
        return filter_listings(listings, max_age_hours=max_age_hours, min_salary=min_salary,
                                include_keywords=title_keywords, exclude_keywords=exclude_title_keywords,
                                allowed_locations=allowed_locations)

    for url in site_urls:
        fetch_result = fetch_career_page(url)
        if not fetch_result.ok:
            failed.append(f"{url} -- {fetch_result.note}")
            _tick(url)
            continue

        listings = extract_job_listings(client, fetch_result.text, url)
        if not listings:
            failed.append(f"{url} -- no job listings extracted")
            _tick(url)
            continue

        listings = _filter(listings)
        for match in score_job_matches(client, resume_text, listings):
            job_store.add_match(match)  # defaults: source_type=career_site, application_method=web_form
            jobs_found += 1
        _tick(url)

    if use_adzuna:
        adzuna_listings = []
        adzuna_failed = []
        for q in queries:
            adzuna = fetch_adzuna_listings(query=q, country=country, min_salary=min_salary,
                                            max_days_old=(max_age_hours // 24 + 1) if max_age_hours else None)
            if not adzuna.ok:
                adzuna_failed.append(f"adzuna:{country}:{q} -- {adzuna.note}")
            else:
                adzuna_listings.extend(adzuna.listings)
        adzuna_listings = _filter(_dedupe_by_url(adzuna_listings))
        failed.extend(adzuna_failed)
        if adzuna_listings:
            for match in score_job_matches(client, resume_text, adzuna_listings):
                job_store.add_match(match, source_type="job_portal_api")
                jobs_found += 1
        _tick("Adzuna")

    if use_remoteok:
        remoteok = fetch_remoteok_listings(tag=queries)
        if not remoteok.ok:
            failed.append(f"remoteok -- {remoteok.note}")
        elif remoteok.listings:
            listings = _filter(remoteok.listings)
            for match in score_job_matches(client, resume_text, listings):
                job_store.add_match(match, source_type="job_portal_api")
                jobs_found += 1
        _tick("RemoteOK")

    for token in greenhouse_boards:
        gh = fetch_greenhouse_listings(token)
        if not gh.ok:
            failed.append(f"greenhouse:{token} -- {gh.note}")
        elif gh.listings:
            # Greenhouse doesn't expose posted-date/salary, so those two checks are a
            # no-op here -- but title_keywords/allowed_locations matter *most* on this
            # source, since a board token pulls the company's entire job list with no
            # query narrowing it at all.
            listings = _filter(gh.listings)
            for match in score_job_matches(client, resume_text, listings):
                job_store.add_match(match, source_type="job_portal_api")
                jobs_found += 1
        _tick(f"Greenhouse: {token}")

    for site in lever_sites:
        lv = fetch_lever_listings(site)
        if not lv.ok:
            failed.append(f"lever:{site} -- {lv.note}")
        elif lv.listings:
            listings = _filter(lv.listings)
            for match in score_job_matches(client, resume_text, listings):
                job_store.add_match(match, source_type="job_portal_api")
                jobs_found += 1
        _tick(f"Lever: {site}")

    return DiscoveryReport(jobs_found=jobs_found, sites_attempted=len(site_urls), sites_failed=failed)
