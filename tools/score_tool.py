"""
Tool: score_job_matches

Scores every job listing from ONE source against the resume, batching
several listings into each LLM call (rather than one call per listing) to
keep cost down and give the model enough context to score consistently
across listings from the same source.

Capped at SCORE_BATCH_SIZE listings per call, chunked as needed -- originally
this was one call per source with no cap, which was fine while sources
returned a handful of listings each, but stopped being fine once Adzuna
started paginating up to 250 results in a single run (see
tools/adzuna_source.py): a very large prompt risks the model truncating its
own structured output partway through, which `zip(jobs, scores)` would then
silently interpret as "the rest scored nothing" rather than surfacing as an
error.

SCORE_BATCH_SIZE trades off two costs that pull in opposite directions:
smaller batches are safer against truncation, but each batch is a full LLM
request, and Gemini's free tier turned out to rate-limit *requests per day*
per model (as low as 20/day observed live for gemini-3.6-flash on a fresh
project -- see the README's free-tier notes) rather than tokens or listings.
250 listings at a batch size of 25 is 10 requests just to score one Adzuna
page; at 100 it's 3. Since the daily request count is the scarcer resource
for most users on the free tier, 100 is the default -- still small enough
that a 258-listing real run (see README) scored correctly in a single
un-chunked call before this limit existed, so 100 has real headroom under
it. If truncation is ever actually observed (see the warning below), lower
this rather than raising the per-source call count back up.

Assumes the model returns scores in the same order the listings were given
within each chunk -- reasonable for a batch this size, but worth hardening
further (e.g. matching by title instead of position) if that ever proves
unreliable in practice. A batch that comes back short (fewer scores than
listings sent) prints a warning rather than silently dropping the
unscored listings, so truncation is at least visible instead of invisible.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import List

from llm_client import LLMClient
from tools.extract_tool import JobListing

SCORE_BATCH_SIZE = 100

SCORE_TOOL_SCHEMA = {
    "name": "record_match_scores",
    "description": "Record a relevance score for each job listing against the candidate's resume.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "score": {"type": "integer", "minimum": 0, "maximum": 100},
                        "rationale": {"type": "string"},
                        "matched_skills": {"type": "array", "items": {"type": "string"}},
                        "missing_skills": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "score", "rationale", "matched_skills", "missing_skills"],
                },
            }
        },
        "required": ["scores"],
    },
}

SYSTEM_PROMPT = (
    "You are a strict technical/product recruiter. Score how well each job "
    "listing matches the candidate's resume, from 0 (no fit) to 100 (ideal fit). "
    "Base scores only on evidence in the resume and listing text provided -- do "
    "not assume skills or experience that are not stated. Be consistent: the "
    "same resume should get a similar score for similarly-scoped roles. Record "
    "your results via the record_match_scores tool, one entry per listing, in "
    "the same order they were given."
)


@dataclass
class MatchResult:
    job: JobListing
    score: int
    rationale: str
    matched_skills: List[str]
    missing_skills: List[str]


def _score_one_batch(client: LLMClient, resume_text: str, jobs: List[JobListing]) -> List[MatchResult]:
    listings_block = "\n\n".join(
        f"[{i}] Title: {j.title}\nLocation: {j.location}\nDescription: {j.snippet}"
        for i, j in enumerate(jobs)
    )
    user_prompt = (
        f"<candidate_resume>\n{resume_text}\n</candidate_resume>\n\n"
        f"<job_listings>\n{listings_block}\n</job_listings>\n\n"
        "Score every listing above against the resume."
    )
    result = client.forced_tool_call(system=SYSTEM_PROMPT, user=user_prompt, tool=SCORE_TOOL_SCHEMA)
    scores = result.get("scores", [])

    if len(scores) < len(jobs):
        print(f"warning: asked to score {len(jobs)} listings but only got {len(scores)} scores back "
              f"-- {len(jobs) - len(scores)} listing(s) will be silently skipped this run, likely "
              f"truncated model output. If this keeps happening, lower SCORE_BATCH_SIZE in "
              f"tools/score_tool.py.", file=sys.stderr)

    matches = []
    for job, s in zip(jobs, scores):
        matches.append(MatchResult(
            job=job,
            score=int(s.get("score", 0)),
            rationale=s.get("rationale", ""),
            matched_skills=s.get("matched_skills", []),
            missing_skills=s.get("missing_skills", []),
        ))
    return matches


def score_job_matches(client: LLMClient, resume_text: str, jobs: List[JobListing]) -> List[MatchResult]:
    if not jobs:
        return []

    matches: List[MatchResult] = []
    for start in range(0, len(jobs), SCORE_BATCH_SIZE):
        batch = jobs[start:start + SCORE_BATCH_SIZE]
        matches.extend(_score_one_batch(client, resume_text, batch))
    return matches
