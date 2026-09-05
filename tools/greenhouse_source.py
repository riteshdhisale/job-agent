"""
Source: Greenhouse Job Board API (official, compliant, no scraping)

Greenhouse publishes an official public API for every company's job board --
confirmed via their own developer docs: "Job Board data is publicly
available, so authentication is not required for any GET endpoints."
This is not a gray area; it's the same kind of source as Adzuna/RemoteOK.

    GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

`board_token` is the slug in a company's public board URL --
boards.greenhouse.io/stripe means board_token="stripe". You maintain the
list of companies you care about in data/greenhouse_boards.json; there's no
global search across all Greenhouse customers.

Already-structured data -- no LLM extraction step needed, straight to
score_job_matches, same pattern as Adzuna/RemoteOK.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import List

import requests

from tools.extract_tool import JobListing

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


@dataclass
class GreenhouseSourceResult:
    ok: bool
    listings: List[JobListing]
    note: str = ""


def _strip_html(text: str) -> str:
    # Some Greenhouse boards return their job description HTML *entity-escaped*
    # inside the JSON string (literal "&lt;div&gt;" text, not a real "<div>" tag) --
    # confirmed live 2026-09-05: every stored Greenhouse snippet was found to be 100%
    # escaped markup eating the whole 500-char snippet budget, starving score_job_matches
    # of any real job-description text. unescape() first so the tag-stripping regex
    # below actually has real "<...>" tags to match; running it on already-plain HTML
    # (no entities) is a harmless no-op.
    text = html.unescape(text or "")
    return re.sub(r"<[^>]+>", " ", text).strip()


def fetch_greenhouse_listings(board_token: str, limit: int = 50) -> GreenhouseSourceResult:
    try:
        resp = requests.get(BASE_URL.format(token=board_token), params={"content": "true"}, timeout=15)
    except requests.RequestException as exc:
        return GreenhouseSourceResult(ok=False, listings=[], note=f"network error: {exc}")

    if resp.status_code == 404:
        return GreenhouseSourceResult(ok=False, listings=[], note=f"no board found for token '{board_token}'")
    if resp.status_code != 200:
        return GreenhouseSourceResult(ok=False, listings=[], note=f"HTTP {resp.status_code}")

    listings = []
    for job in resp.json().get("jobs", [])[:limit]:
        listings.append(JobListing(
            title=job.get("title", "").strip(),
            location=(job.get("location") or {}).get("name", ""),
            url=job.get("absolute_url", ""),
            snippet=_strip_html(job.get("content", ""))[:500],
            source_url=f"greenhouse:{board_token}",
        ))
    return GreenhouseSourceResult(ok=True, listings=listings)
