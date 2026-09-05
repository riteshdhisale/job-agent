"""
Tool: tailor_resume (Agent 2)

Rewrites resume bullets to better match a specific job -- but only for jobs
that already cleared the >=90 match-score gate from Agent 1's scoring tool.
This gate is not a formality: the research in 02_business_strategy.md
(section 2) found mass-apply tools get 2-4% callback rates specifically
because they apply everywhere with a generic document. Tailoring effort
only pays off, and only avoids becoming the same spam pattern, if it's
reserved for jobs that are genuinely a strong fit.

HARD RULE: this tool may rephrase, reorder, and re-emphasize existing
resume content -- it may never invent an employer, title, skill, metric, or
accomplishment that isn't already in the source resume. Anything the job
needs that the resume doesn't support goes in flagged_gaps instead, so you
walk in prepared rather than caught out (this feeds Agent 5's interview prep).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from llm_client import LLMClient

MIN_MATCH_SCORE_TO_TAILOR = 90

TAILOR_TOOL_SCHEMA = {
    "name": "record_tailored_resume",
    "description": "Record a tailored version of the resume for one specific job.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tailored_summary": {"type": "string"},
            "tailored_bullets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "original": {"type": "string"},
                        "tailored": {"type": "string"},
                        "reason": {"type": "string", "description": "Why this change helps for this specific job."},
                    },
                    "required": ["original", "tailored", "reason"],
                },
            },
            "flagged_gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Requirements in the job the resume genuinely does not cover. Do not paper over these.",
            },
        },
        "required": ["tailored_summary", "tailored_bullets", "flagged_gaps"],
    },
}

SYSTEM_PROMPT = (
    "You tailor a resume for one specific job posting. HARD RULE: you may "
    "rephrase, reorder, re-emphasize, and select which existing bullets to "
    "foreground -- you may NEVER invent an employer, title, skill, metric, or "
    "accomplishment that is not already present in the source resume. If the "
    "job needs something the resume doesn't show evidence of, list it in "
    "flagged_gaps instead of fabricating it. Record your output via "
    "record_tailored_resume."
)


class MatchScoreTooLowError(Exception):
    pass


@dataclass
class TailoredBullet:
    original: str
    tailored: str
    reason: str


@dataclass
class TailorResult:
    tailored_summary: str
    tailored_bullets: List[TailoredBullet]
    flagged_gaps: List[str]


def tailor_resume(client: LLMClient, resume_text: str, job_title: str, job_snippet: str,
                   match_score: int, force: bool = False) -> TailorResult:
    if match_score < MIN_MATCH_SCORE_TO_TAILOR and not force:
        raise MatchScoreTooLowError(
            f"score {match_score} is below the {MIN_MATCH_SCORE_TO_TAILOR} gate -- tailoring "
            f"a resume for a weak match wastes effort and risks the same low-relevance, "
            f"low-callback pattern the mass-apply tools are known for. Pass force=True to override."
        )

    user_prompt = (
        f"<candidate_resume>\n{resume_text}\n</candidate_resume>\n\n"
        f"<target_job>\nTitle: {job_title}\nDescription: {job_snippet}\n</target_job>\n\n"
        "Tailor this resume for this specific job."
    )
    result = client.forced_tool_call(system=SYSTEM_PROMPT, user=user_prompt, tool=TAILOR_TOOL_SCHEMA)
    return TailorResult(
        tailored_summary=result.get("tailored_summary", ""),
        tailored_bullets=[TailoredBullet(**b) for b in result.get("tailored_bullets", [])],
        flagged_gaps=result.get("flagged_gaps", []),
    )
