"""
Source: RemoteOK public feed

A fully open, no-auth JSON feed of remote job listings -- no API key, no
signup. Confirmed live at https://remoteok.com/api. Skews heavily toward
software engineering roles, but is worth including for remote-friendly
tech/product roles and costs nothing to query. Courtesy note from RemoteOK:
attribute them and link back to the original post if you republish this
data anywhere public (not a concern for a private personal tool like this).

Like Adzuna, this feed is already structured -- no LLM extraction needed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Union

import requests

from tools.extract_tool import JobListing

FEED_URL = "https://remoteok.com/api"
USER_AGENT = "Mozilla/5.0 (compatible; JobAgent/0.1; personal use; contact: your-email@example.com)"


@dataclass
class RemoteOKSourceResult:
    ok: bool
    listings: List[JobListing]
    note: str = ""


def fetch_remoteok_listings(tag: Union[str, Sequence[str]] = "product", limit: int = 20) -> RemoteOKSourceResult:
    """`tag` accepts either one keyword or several (e.g. searching for Product Manager,
    Product Owner, and Business Analyst roles in the same run) -- a listing matches if
    ANY of them appears in its tag list or title. Passing several tags still only fetches
    the feed once (it's the same request either way), and each matching listing appears
    at most once even if it matches more than one tag."""
    tags_wanted = [tag] if isinstance(tag, str) else list(tag)
    tags_wanted = [t.lower() for t in tags_wanted if t]

    try:
        resp = requests.get(FEED_URL, headers={"User-Agent": USER_AGENT}, timeout=15)
    except requests.RequestException as exc:
        return RemoteOKSourceResult(ok=False, listings=[], note=f"network error: {exc}")

    if resp.status_code != 200:
        return RemoteOKSourceResult(ok=False, listings=[], note=f"HTTP {resp.status_code}")

    try:
        entries = resp.json()
    except ValueError:
        return RemoteOKSourceResult(ok=False, listings=[], note="response was not valid JSON")

    listings = []
    for entry in entries:
        if not isinstance(entry, dict) or "position" not in entry:
            continue  # the feed's first element is a legal/notice object, not a job -- skip anything malformed
        entry_tags = [t.lower() for t in entry.get("tags", [])]
        position = entry.get("position", "").lower()
        if tags_wanted and not any(t in entry_tags or t in position for t in tags_wanted):
            continue
        listings.append(JobListing(
            title=entry.get("position", "").strip(),
            location=entry.get("location", "Remote") or "Remote",
            url=entry.get("url", ""),
            snippet=(entry.get("description", "") or "")[:500],
            source_url="remoteok",
        ))
        if len(listings) >= limit:
            break

    return RemoteOKSourceResult(ok=True, listings=listings)
