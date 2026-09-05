"""
Tool: fetch_career_page

Fetches a career site page (or a local HTML fixture) and returns cleaned
plain text an LLM can read. This is the ONLY place in the codebase that
touches the network or filesystem for source pages -- keeping it isolated
means swapping in a headless browser (Playwright) later, for JS-rendered
sites, only touches this one file.

See the primer, section 8, for why this fails a lot in practice (bot
detection, JS-only pages) and why that's a real product decision, not a bug
to just code around.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (compatible; JobMatchAgent/0.1; learning project; "
    "contact: your-email@example.com)"
)

# A page that renders almost nothing server-side is almost certainly a
# JavaScript single-page app we can't scrape with a plain GET.
MIN_USEFUL_TEXT_CHARS = 200
MAX_TEXT_CHARS = 15_000  # keep prompts (and cost) bounded


@dataclass
class FetchResult:
    url: str
    ok: bool
    text: str
    status_code: Optional[int]
    note: str = ""


def fetch_career_page(url: str, timeout: int = 15) -> FetchResult:
    """Fetch a career page. `url` may be:
      - an http(s):// URL (real network fetch), or
      - a local file path, with or without a `file://` prefix (used by the
        sample fixtures in data/sample_pages/, so you can run this whole
        project with zero network access first).
    """
    if url.startswith("http://") or url.startswith("https://"):
        return _fetch_http(url, timeout)
    path = url[len("file://"):] if url.startswith("file://") else url
    return _fetch_local_file(path, original_url=url)


def _fetch_http(url: str, timeout: int) -> FetchResult:
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    except requests.RequestException as exc:
        return FetchResult(url=url, ok=False, text="", status_code=None, note=f"network error: {exc}")

    if resp.status_code == 403:
        return FetchResult(
            url=url, ok=False, text="", status_code=403,
            note="blocked (403) -- this site is likely rate-limiting or "
                 "fingerprinting generic scrapers (common on ATS platforms)",
        )
    if resp.status_code >= 400:
        return FetchResult(url=url, ok=False, text="", status_code=resp.status_code,
                            note=f"HTTP error {resp.status_code}")

    return _clean_html(resp.text, url, resp.status_code)


def _fetch_local_file(path: str, original_url: str) -> FetchResult:
    try:
        raw_html = Path(path).read_text()
    except OSError as exc:
        return FetchResult(url=original_url, ok=False, text="", status_code=None,
                            note=f"local file error: {exc}")
    return _clean_html(raw_html, original_url, status_code=200)


def _clean_html(raw_html: str, url: str, status_code: Optional[int]) -> FetchResult:
    soup = BeautifulSoup(raw_html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = " ".join(soup.get_text(separator=" ").split())

    if len(text) < MIN_USEFUL_TEXT_CHARS:
        return FetchResult(
            url=url, ok=False, text=text, status_code=status_code,
            note="page rendered almost no text -- likely a JavaScript-rendered "
                 "SPA (needs a headless browser, not a plain GET)",
        )

    return FetchResult(url=url, ok=True, text=text[:MAX_TEXT_CHARS], status_code=status_code)
