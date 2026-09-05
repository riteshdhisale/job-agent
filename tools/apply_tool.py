"""
Tools: fill_application_form and draft_application_email (Agent 4)

A job's application_method (see job_store.py) decides which of these runs:

  'web_form' -- most career-site jobs. fill_application_form opens the page
      with Playwright, fills whatever fields it can confidently map to the
      Candidate Profile, and STOPS -- it never clicks submit. Fields it
      can't map come back as `unmatched_fields` for cli.py to ask you about
      once; your answer is then saved to the profile so it's never asked
      again on a future application.

  'email' -- LinkedIn posts that say "send your resume to x@company.com".
      draft_application_email writes the email for you to review, attach
      your resume to, and send yourself. Never sent automatically.

  'link' / 'dm' / 'unclear' -- cli.py surfaces these to you directly rather
      than guessing; a raw link is often just another web form (try
      fill_application_form on it) but a DM instruction has no safe
      automation path consistent with the compliant-only decision.

Field matching in fill_application_form is label-based and deliberately
conservative: a field only gets auto-filled if its label matches a known
alias with reasonable confidence. Everything else is surfaced, never
guessed -- a wrong guess on a screening question is worse than a question
you have to answer once.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from llm_client import LLMClient
from profile_store import CandidateProfile

# normalized label text -> CandidateProfile-derived key (see _profile_as_dict)
FIELD_ALIASES: Dict[str, str] = {
    "first name": "first_name", "given name": "first_name",
    "last name": "last_name", "surname": "last_name", "family name": "last_name",
    "full name": "full_name", "name": "full_name",
    "email": "email", "email address": "email",
    "phone": "phone", "phone number": "phone", "mobile": "phone", "mobile number": "phone",
    "linkedin": "linkedin_url", "linkedin url": "linkedin_url", "linkedin profile": "linkedin_url",
    "portfolio": "portfolio_url", "website": "portfolio_url", "portfolio url": "portfolio_url",
    "location": "location", "current location": "location", "city": "location",
}


@dataclass
class FilledField:
    label: str
    value: str
    source: str  # "profile" or "cached_answer"


@dataclass
class ApplyResult:
    filled: List[FilledField] = field(default_factory=list)
    unmatched_fields: List[str] = field(default_factory=list)
    screenshot_path: Optional[str] = None


def fill_application_form(url: str, profile: CandidateProfile, screenshot_path: Optional[str] = None,
                           headless: bool = True) -> ApplyResult:
    from playwright.sync_api import sync_playwright  # imported lazily -- not needed for email-only setups

    result = ApplyResult()
    profile_values = _profile_as_dict(profile)
    nav_url = _to_navigable_url(url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.goto(nav_url, wait_until="domcontentloaded", timeout=20000)

        # Every <label> paired with a text/email/tel input or textarea is a
        # candidate field. This is a heuristic, not a guarantee: some ATS
        # platforms (notably Workday) render fully custom components instead
        # of real <label>/<input> pairs, and this will legitimately find
        # nothing there -- a real limitation to plan around (see README),
        # not a bug to silently paper over.
        for label_el in page.locator("label").all():
            label_text = (label_el.inner_text() or "").strip()
            if not label_text:
                continue
            normalized = _normalize(label_text)
            input_el = _resolve_input_for_label(page, label_el)
            if input_el is None:
                continue

            profile_key = FIELD_ALIASES.get(normalized)
            if profile_key and profile_values.get(profile_key):
                if _try_fill(input_el, profile_values[profile_key]):
                    result.filled.append(FilledField(label_text, profile_values[profile_key], "profile"))
                    continue

            cached = profile.get(label_text)
            if cached and _try_fill(input_el, cached):
                result.filled.append(FilledField(label_text, cached, "cached_answer"))
                continue

            result.unmatched_fields.append(label_text)

        if screenshot_path:
            page.screenshot(path=screenshot_path, full_page=True)
            result.screenshot_path = screenshot_path

        browser.close()

    return result


def _to_navigable_url(url: str) -> str:
    """Playwright's goto() needs a real URL scheme. http(s):// URLs pass
    through unchanged; anything else (including a bare relative path, used
    by the local sample_pages/ fixtures) is treated as a local file and
    converted to an absolute file:// URI -- same convention as fetch_tool."""
    if url.startswith("http://") or url.startswith("https://") or url.startswith("file://"):
        return url
    return Path(url).resolve().as_uri()


def _try_fill(input_el, value: str) -> bool:
    try:
        input_el.fill(value)
        return True
    except Exception:
        return False  # e.g. a non-fillable custom widget -- fall through to "unmatched" rather than crash the run


def _resolve_input_for_label(page, label_el):
    for_attr = label_el.get_attribute("for")
    if for_attr:
        try:
            el = page.locator(f"#{for_attr}")
            if el.count() > 0:
                return el.first
        except Exception:
            pass
    try:
        nested = label_el.locator("input, textarea")
        if nested.count() > 0:
            return nested.first
    except Exception:
        pass
    return None


def _profile_as_dict(profile: CandidateProfile) -> Dict[str, str]:
    full_name = profile.full_name or ""
    parts = full_name.split(" ", 1)
    return {
        "full_name": full_name,
        "first_name": parts[0] if parts else "",
        "last_name": parts[1] if len(parts) > 1 else "",
        "email": profile.email, "phone": profile.phone,
        "linkedin_url": profile.linkedin_url, "portfolio_url": profile.portfolio_url,
        "location": profile.location,
    }


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().rstrip("*: ").split())


# --- email-based applications (LinkedIn posts that say "email your resume to...") ---

APPLICATION_EMAIL_SCHEMA = {
    "name": "record_application_email",
    "description": "Record a draft email applying for a job by email.",
    "input_schema": {
        "type": "object",
        "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
        "required": ["subject", "body"],
    },
}

APPLICATION_EMAIL_SYSTEM_PROMPT = (
    "You draft a concise, professional email applying for a job, to be sent by "
    "the candidate themselves with their resume attached. Reference the role and "
    "one concrete matching point from the provided match rationale. Mention that "
    "the resume is attached. Keep it under 150 words. This is a DRAFT for human "
    "review -- never claim to have already sent it. Record via record_application_email."
)


@dataclass
class ApplicationEmailDraft:
    subject: str
    body: str
    to: str


def draft_application_email(client: LLMClient, job_title: str, company: str,
                             match_rationale: str, target_email: str) -> ApplicationEmailDraft:
    user_prompt = (
        f"<target_role>\nCompany: {company}\nTitle: {job_title}\n</target_role>\n\n"
        f"<why_this_is_a_match>\n{match_rationale}\n</why_this_is_a_match>\n\n"
        f"<apply_to_email>\n{target_email}\n</apply_to_email>\n\n"
        "Draft the application email."
    )
    result = client.forced_tool_call(system=APPLICATION_EMAIL_SYSTEM_PROMPT, user=user_prompt,
                                      tool=APPLICATION_EMAIL_SCHEMA)
    return ApplicationEmailDraft(subject=result.get("subject", ""), body=result.get("body", ""), to=target_email)
