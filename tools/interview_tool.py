"""
Tool: prepare_for_interview (Agent 5)

Generates likely interview questions and coaching notes for one specific
job, using the tailored resume, Agent 1's original match rationale (why
this job was a good fit), and Agent 2's flagged_gaps (what an interviewer
is most likely to probe, since the resume doesn't fully cover it). Trigger
this manually once you're actually shortlisted -- see cli.py's `prep` command.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from llm_client import LLMClient

PREP_TOOL_SCHEMA = {
    "name": "record_interview_prep",
    "description": "Record interview preparation notes for one job.",
    "input_schema": {
        "type": "object",
        "properties": {
            "likely_questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "why_theyll_ask": {"type": "string"},
                        "how_to_answer": {
                            "type": "string",
                            "description": "A coaching note pointing to which resume evidence to use -- not a script to recite.",
                        },
                    },
                    "required": ["question", "why_theyll_ask", "how_to_answer"],
                },
            },
            "questions_to_ask_them": {"type": "array", "items": {"type": "string"}},
            "watch_out_for": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The known gaps, reframed as things to prepare an honest answer for.",
            },
        },
        "required": ["likely_questions", "questions_to_ask_them", "watch_out_for"],
    },
}

SYSTEM_PROMPT = (
    "You prepare a candidate for a specific job interview. Generate likely "
    "interview questions grounded in the actual job description and the "
    "candidate's tailored resume -- not a generic interview-question list. "
    "For each question, note why this interviewer would likely ask it, and "
    "coach the candidate on which of their own resume evidence to draw on -- "
    "do not write a verbatim script for them to recite. Also reframe the "
    "candidate's known gaps (provided below) as things to prepare an honest, "
    "confident answer for, rather than hope nobody asks. Suggest a few sharp "
    "questions the candidate should ask the interviewer. Record everything "
    "via record_interview_prep."
)


@dataclass
class InterviewQuestion:
    question: str
    why_theyll_ask: str
    how_to_answer: str


@dataclass
class InterviewPrep:
    likely_questions: List[InterviewQuestion]
    questions_to_ask_them: List[str]
    watch_out_for: List[str]


def prepare_for_interview(client: LLMClient, tailored_resume_summary: str, job_title: str,
                           job_snippet: str, match_rationale: str, flagged_gaps: List[str]) -> InterviewPrep:
    user_prompt = (
        f"<tailored_resume_summary>\n{tailored_resume_summary}\n</tailored_resume_summary>\n\n"
        f"<job>\nTitle: {job_title}\nDescription: {job_snippet}\n</job>\n\n"
        f"<why_matched>\n{match_rationale}\n</why_matched>\n\n"
        f"<known_gaps>\n{', '.join(flagged_gaps) or 'none flagged'}\n</known_gaps>\n\n"
        "Prepare this candidate for the interview."
    )
    result = client.forced_tool_call(system=SYSTEM_PROMPT, user=user_prompt, tool=PREP_TOOL_SCHEMA)
    return InterviewPrep(
        likely_questions=[InterviewQuestion(**q) for q in result.get("likely_questions", [])],
        questions_to_ask_them=result.get("questions_to_ask_them", []),
        watch_out_for=result.get("watch_out_for", []),
    )
