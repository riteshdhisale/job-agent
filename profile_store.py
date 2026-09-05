"""
Candidate Profile store.

A single persistent JSON file holding everything about you that would
otherwise get re-asked on every application: contact details, work
authorization, and a growing cache of answers to recurring screening
questions ("Are you authorized to work in X?", "Why do you want to work
here?", etc). Every agent in this package reads this before asking you
anything and writes back any new answer -- this is the piece of shared
memory the whole system is built around (see 02_business_strategy.md,
section 5, and the primer's note on state/memory).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Optional

DEFAULT_PROFILE_PATH = Path(__file__).resolve().parent / "data" / "profile.json"


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


@dataclass
class CandidateProfile:
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin_url: str = ""
    portfolio_url: str = ""
    work_authorized: str = ""       # free text, e.g. "Yes, no sponsorship needed"
    resume_path: str = ""
    answers: Dict[str, str] = field(default_factory=dict)  # normalized question -> answer

    def get(self, question: str) -> Optional[str]:
        return self.answers.get(_normalize(question))

    def remember(self, question: str, answer: str) -> None:
        self.answers[_normalize(question)] = answer


class ProfileStore:
    def __init__(self, path: Path = DEFAULT_PROFILE_PATH):
        self.path = path

    def load(self) -> CandidateProfile:
        if not self.path.exists():
            return CandidateProfile()
        data = json.loads(self.path.read_text())
        # Tolerate old/partial files gracefully rather than crashing on a
        # missing key -- this file will keep growing new fields over time.
        known_fields = {f for f in CandidateProfile.__dataclass_fields__}
        return CandidateProfile(**{k: v for k, v in data.items() if k in known_fields})

    def save(self, profile: CandidateProfile) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(profile), indent=2))
