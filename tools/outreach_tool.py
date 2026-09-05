"""
Tool: draft_outreach_message (Agent 3, part 2)

Drafts a short, personalized outreach note referencing the specific job and
one concrete matching point pulled from Agent 1's match rationale -- never
invented. ALWAYS a draft: per the compliant-only decision
(02_business_strategy.md, section 4), this tool never sends anything.
cli.py's `outreach` command prints the draft and stops there.
"""
from __future__ import annotations

from dataclasses import dataclass

from llm_client import LLMClient

DRAFT_TOOL_SCHEMA = {
    "name": "record_outreach_draft",
    "description": "Record a short, personalized outreach message draft.",
    "input_schema": {
        "type": "object",
        "properties": {
            "subject_line": {"type": "string"},
            "message": {"type": "string", "description": "150 words or fewer. Warm, specific, no generic flattery."},
        },
        "required": ["subject_line", "message"],
    },
}

SYSTEM_PROMPT = (
    "You draft a short, genuinely personalized outreach message from a job "
    "candidate to someone at a company they're interested in. Reference the "
    "specific role and ONE concrete matching point from the candidate's "
    "background -- pull it from the provided match rationale, don't invent "
    "one. No generic flattery ('I've always admired your company'), no "
    "over-long message, no assumption the reader owes a reply. End with a "
    "low-pressure, specific ask. Record it via record_outreach_draft. This is "
    "a DRAFT for the candidate to review and send themselves -- never claim "
    "to have already sent anything."
)


@dataclass
class OutreachDraft:
    subject_line: str
    message: str
    recipient_name: str
    recipient_title: str


def draft_outreach_message(client: LLMClient, resume_text: str, job_title: str, company_name: str,
                            match_rationale: str, recipient_name: str, recipient_title: str) -> OutreachDraft:
    user_prompt = (
        f"<candidate_resume>\n{resume_text}\n</candidate_resume>\n\n"
        f"<target_role>\nCompany: {company_name}\nTitle: {job_title}\n</target_role>\n\n"
        f"<why_this_is_a_match>\n{match_rationale}\n</why_this_is_a_match>\n\n"
        f"<recipient>\nName: {recipient_name}\nTitle: {recipient_title}\n</recipient>\n\n"
        "Draft a short outreach message to this recipient about this role."
    )
    result = client.forced_tool_call(system=SYSTEM_PROMPT, user=user_prompt, tool=DRAFT_TOOL_SCHEMA)
    return OutreachDraft(
        subject_line=result.get("subject_line", ""), message=result.get("message", ""),
        recipient_name=recipient_name, recipient_title=recipient_title,
    )
