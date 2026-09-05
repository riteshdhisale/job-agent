"""Tests for Agent 4's Playwright form-filler, against a local fixture --
no network needed. Verifies: profile fields get auto-filled, fields with no
confident match are surfaced (never guessed), and the submit button is
never touched (this test doesn't even give it the chance to)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from profile_store import CandidateProfile
from tools.apply_tool import fill_application_form

FIXTURE = str(Path(__file__).resolve().parent.parent / "data" / "sample_pages" / "example_application_form.html")


def make_profile():
    return CandidateProfile(
        full_name="Jane Doe", email="jane.doe@example.com", phone="555-0142",
        location="Pune, Maharashtra, India", linkedin_url="https://www.linkedin.com/in/jane-doe-example",
    )


def test_fills_known_fields_from_profile(tmp_path):
    result = fill_application_form(FIXTURE, make_profile(), screenshot_path=str(tmp_path / "shot.png"))
    filled_labels = {f.label for f in result.filled}
    assert "First Name" in filled_labels
    assert "Last Name" in filled_labels
    assert "Email" in filled_labels
    assert "Phone Number" in filled_labels
    assert "LinkedIn URL" in filled_labels
    assert "Current Location" in filled_labels


def test_first_and_last_name_split_correctly():
    result = fill_application_form(FIXTURE, make_profile())
    values = {f.label: f.value for f in result.filled}
    assert values["First Name"] == "Jane"
    assert values["Last Name"] == "Doe"


def test_unmapped_questions_are_surfaced_not_guessed():
    result = fill_application_form(FIXTURE, make_profile())
    assert "Are you legally authorized to work in this location?" in result.unmatched_fields
    assert "Why do you want to work here?" in result.unmatched_fields


def test_cached_answer_is_used_on_a_later_application():
    profile = make_profile()
    profile.remember("Are you legally authorized to work in this location?", "Yes, no sponsorship needed")
    result = fill_application_form(FIXTURE, profile)
    filled_labels = {f.label: f.value for f in result.filled}
    assert filled_labels.get("Are you legally authorized to work in this location?") == "Yes, no sponsorship needed"
    assert "Are you legally authorized to work in this location?" not in result.unmatched_fields


def test_screenshot_is_saved(tmp_path):
    screenshot_path = tmp_path / "shot.png"
    result = fill_application_form(FIXTURE, make_profile(), screenshot_path=str(screenshot_path))
    assert screenshot_path.exists()
    assert result.screenshot_path == str(screenshot_path)
