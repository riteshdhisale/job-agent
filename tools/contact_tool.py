"""
Tool: find_contacts (Agent 3, part 1)

Finds people to reach out to at a target company, using Apollo.io's People
Search API -- a ToS-compliant alternative to scraping LinkedIn directly
(see 02_business_strategy.md, section 4, for why that distinction matters
for anything beyond a private one-off script). Sign up for Apollo's free
tier (75 lookups/month, no cost) to try this: https://www.apollo.io

Verify the endpoint/field names below against Apollo's current API docs
(https://apolloio.github.io/apollo-api-docs/) before relying on this --
this was written from published documentation, not tested against a live
key, since no ANTHROPIC/APOLLO key was available while building it.

Strategy: look for people in a recruiting/talent-acquisition role first
(most likely to actually read a cold note about an open role); fall back
to the job's own function/department if nothing recruiting-shaped turns
up. Always returns a LIST, never a single confident "the hiring manager"
guess -- a people-search API can't promise that reliably, so this is
honest about the fallback instead of pretending precision it doesn't have.

Note: if the job came from a pasted LinkedIn post (source_type ==
'linkedin_post'), it likely already HAS a contact -- the post's author is
often the actual hiring manager. Check job.outreach_contacts before calling
this at all; see cli.py's `contacts` command.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import requests

APOLLO_SEARCH_URL = "https://api.apollo.io/v1/mixed_people/search"
MAX_CONTACTS = 5
RECRUITING_TITLES = ["recruiter", "talent acquisition", "recruiting", "people team", "hr business partner"]


@dataclass
class Contact:
    name: str
    title: str
    linkedin_url: str
    email: Optional[str]
    source: str  # "apollo" or "linkedin_post"


def find_contacts(company_name: str, department_hint: str = "", api_key: Optional[str] = None) -> List[Contact]:
    key = api_key or os.environ.get("APOLLO_API_KEY")
    if not key:
        return []  # caller decides how to handle "no contacts found" -- see cli.py `contacts`

    contacts: List[Contact] = []
    title_batches = [RECRUITING_TITLES] + ([[department_hint]] if department_hint else [])
    for titles in title_batches:
        if len(contacts) >= MAX_CONTACTS:
            break
        try:
            resp = requests.post(
                APOLLO_SEARCH_URL,
                headers={"Content-Type": "application/json", "X-Api-Key": key},
                json={"organization_name": company_name, "person_titles": titles,
                      "per_page": MAX_CONTACTS - len(contacts)},
                timeout=15,
            )
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        for person in resp.json().get("people", []):
            if len(contacts) >= MAX_CONTACTS:
                break
            contacts.append(Contact(
                name=person.get("name", ""), title=person.get("title", ""),
                linkedin_url=person.get("linkedin_url", ""), email=person.get("email"), source="apollo",
            ))
    return contacts
