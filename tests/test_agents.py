"""Tests for Agents 2, 3, and 5's tools using FakeLLMClient -- no API key
or network needed. These verify wiring and hard rules (the 90-score gate),
not real output quality -- see llm_client.FakeLLMClient's docstring."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from llm_client import FakeLLMClient
from tools.tailor_tool import tailor_resume, MatchScoreTooLowError, MIN_MATCH_SCORE_TO_TAILOR
from tools.outreach_tool import draft_outreach_message
from tools.interview_tool import prepare_for_interview
from tools.manual_lead_tool import parse_pasted_lead

RESUME = "Product Manager with 6+ years of SaaS and API platform experience."


def test_tailor_rejects_low_score():
    client = FakeLLMClient()
    with pytest.raises(MatchScoreTooLowError):
        tailor_resume(client, RESUME, "Product Manager", "a role", match_score=MIN_MATCH_SCORE_TO_TAILOR - 1)


def test_tailor_accepts_score_at_gate():
    client = FakeLLMClient()
    result = tailor_resume(client, RESUME, "Product Manager", "a role", match_score=MIN_MATCH_SCORE_TO_TAILOR)
    assert result.tailored_summary


def test_tailor_force_overrides_gate():
    client = FakeLLMClient()
    result = tailor_resume(client, RESUME, "Product Manager", "a role", match_score=10, force=True)
    assert result.tailored_summary


def test_outreach_draft_references_recipient():
    client = FakeLLMClient()
    draft = draft_outreach_message(client, RESUME, "Product Manager", "Acme Inc",
                                    "strong API/SaaS overlap", "Jane Doe", "Engineering Manager")
    assert draft.recipient_name == "Jane Doe"
    assert draft.subject_line and draft.message


def test_interview_prep_returns_structured_questions():
    client = FakeLLMClient()
    prep = prepare_for_interview(client, RESUME, "Product Manager", "a role",
                                  "strong overlap", flagged_gaps=["no formal SQL certification"])
    assert len(prep.likely_questions) >= 1
    assert prep.likely_questions[0].question


def test_manual_lead_detects_job_posting_with_email():
    client = FakeLLMClient()
    text = ("We're hiring a Senior Product Manager! Send your resume to jobs@acme.example "
            "if you're interested.")
    lead = parse_pasted_lead(client, text, source_label="linkedin_post")
    assert lead.is_job_posting
    assert lead.application_method == "email"
    assert lead.application_target == "jobs@acme.example"
    assert "Manager" in lead.listing.title


def test_manual_lead_ignores_non_job_text():
    client = FakeLLMClient()
    lead = parse_pasted_lead(client, "Happy Friday everyone, hope you all have a great weekend!")
    assert not lead.is_job_posting
    assert lead.listing is None
