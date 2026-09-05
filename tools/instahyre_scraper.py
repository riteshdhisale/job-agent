"""
Source: Instahyre (scraping -- accepted risk, rate-limited)

Unlike Greenhouse/Lever/Adzuna/RemoteOK, Instahyre has no public API, so
this reads their search-results HTML directly. This is the "go all in"
category you chose for Instahyre specifically -- checked before building
this: Instahyre's robots.txt has NO disallow rules for any user-agent, so
this doesn't run against their own stated crawling preference the way
Naukri's does (see naukri_note.md in this same folder for why there's no
equivalent naukri_scraper.py).

Still real risk, just a different kind than LinkedIn's: no known account-
ban mechanism here since this doesn't use a logged-in session, but the
page structure below is BEST-EFFORT and UNVERIFIED against the live site
(this was built in a sandbox that cannot reach instahyre.com) -- expect to
open your browser's dev tools and adjust the CSS selectors on first run.
That's not a caveat to wave away; treat the first run as a debugging
session, not a working feature.

Enforces the "a few times a day" cadence you chose via rate_limit.py --
call fetch_instahyre_listings() and it will refuse to run again inside the
cooldown window unless you pass force=True.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import requests
from bs4 import BeautifulSoup

from tools.extract_tool import JobListing
from tools.rate_limit import check_and_record, CooldownActive

SEARCH_URL = "https://www.instahyre.com/search-jobs/"
USER_AGENT = "Mozilla/5.0 (compatible; personal job search tool; contact: your-email@example.com)"
SOURCE_NAME = "instahyre"


@dataclass
class InstahyreSourceResult:
    ok: bool
    listings: List[JobListing]
    note: str = ""


def fetch_instahyre_listings(query: str = "product manager", force: bool = False) -> InstahyreSourceResult:
    try:
        check_and_record(SOURCE_NAME, force=force)
    except CooldownActive as e:
        return InstahyreSourceResult(ok=False, listings=[], note=str(e))

    try:
        resp = requests.get(SEARCH_URL, params={"q": query}, headers={"User-Agent": USER_AGENT}, timeout=15)
    except requests.RequestException as exc:
        return InstahyreSourceResult(ok=False, listings=[], note=f"network error: {exc}")

    if resp.status_code != 200:
        return InstahyreSourceResult(ok=False, listings=[], note=f"HTTP {resp.status_code}")

    soup = BeautifulSoup(resp.text, "lxml")
    # BEST-EFFORT selectors -- Instahyre's actual markup wasn't reachable while
    # building this. Job cards are commonly rendered as <a> or <div> elements
    # carrying a "job" or "opportunity" class; if this returns nothing, open
    # the page in a browser, inspect a job card, and update this selector.
    cards = soup.select("[class*=job], [class*=opportunity]")

    if not cards:
        return InstahyreSourceResult(
            ok=False, listings=[],
            note="no job cards matched the selector -- either no results, a login wall, "
                 "a JS-rendered page (this uses a plain GET, no JS execution), or Instahyre's "
                 "markup no longer matches this best-effort selector. Inspect the live page "
                 "and update tools/instahyre_scraper.py's `cards` selector.",
        )

    listings = []
    for card in cards:
        title_el = card.find(["h2", "h3", "a"])
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            continue
        link_el = card.find("a", href=True)
        url = link_el["href"] if link_el else SEARCH_URL
        if url.startswith("/"):
            url = "https://www.instahyre.com" + url
        listings.append(JobListing(
            title=title, location="(unverified selector -- location not reliably parsed)",
            url=url, snippet=card.get_text(" ", strip=True)[:300], source_url="instahyre",
        ))

    return InstahyreSourceResult(ok=True, listings=listings)
