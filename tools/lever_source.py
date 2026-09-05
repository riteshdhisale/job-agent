"""
Source: Lever Postings API (official, compliant, no scraping)

Same category as Greenhouse: Lever's own public API, documented at
https://github.com/lever/postings-api. Confirmed: "The GET request for
listing is publicly accessible" -- no API key needed to read postings.

    GET https://api.lever.co/v0/postings/{site}?mode=json

`site` is the slug in a company's public postings URL --
jobs.lever.co/acme means site="acme". Maintain the list of companies you
care about in data/lever_sites.json. Already-structured JSON -- no LLM
extraction needed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import requests

from tools.extract_tool import JobListing

BASE_URL = "https://api.lever.co/v0/postings/{site}"


@dataclass
class LeverSourceResult:
    ok: bool
    listings: List[JobListing]
    note: str = ""


def fetch_lever_listings(site: str, limit: int = 50) -> LeverSourceResult:
    try:
        resp = requests.get(BASE_URL.format(site=site), params={"mode": "json"}, timeout=15)
    except requests.RequestException as exc:
        return LeverSourceResult(ok=False, listings=[], note=f"network error: {exc}")

    if resp.status_code != 200:
        return LeverSourceResult(ok=False, listings=[], note=f"HTTP {resp.status_code} for site '{site}'")

    postings = resp.json()
    if not isinstance(postings, list):
        return LeverSourceResult(ok=False, listings=[], note="unexpected response shape")

    listings = []
    for job in postings[:limit]:
        categories = job.get("categories", {}) or {}
        listings.append(JobListing(
            title=job.get("text", "").strip(),
            location=categories.get("location", ""),
            url=job.get("hostedUrl", ""),
            snippet=(job.get("descriptionPlain", "") or "")[:500],
            source_url=f"lever:{site}",
        ))
    return LeverSourceResult(ok=True, listings=listings)
