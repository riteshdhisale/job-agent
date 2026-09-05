"""
A tiny cooldown helper so the higher-effort sources (scraping, or anything
hitting a site more aggressively than official APIs) actually run at the
cadence you decided on -- "a few times a day, like checking manually" --
rather than however often a script or schedule happens to invoke them.
This turns that decision into an enforced property of the tool, not just a
README suggestion you have to remember to respect yourself.

State lives in a small JSON file, one timestamp per named source.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict

DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "scrape_cooldowns.json"
DEFAULT_COOLDOWN_SECONDS = 3 * 60 * 60  # 3 hours -- roughly "a few times a day"


class CooldownActive(Exception):
    def __init__(self, source: str, seconds_remaining: float):
        self.source = source
        self.seconds_remaining = seconds_remaining
        minutes = int(seconds_remaining // 60)
        super().__init__(
            f"'{source}' was checked less than {DEFAULT_COOLDOWN_SECONDS // 3600}h ago "
            f"({minutes} min remaining). This cooldown exists on purpose -- see the "
            f"'check frequency' decision in the README. Pass force=True to override "
            f"just this once if you genuinely need to."
        )


def _load(path: Path) -> Dict[str, float]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(path: Path, state: Dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def check_and_record(source: str, cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
                      force: bool = False, state_path: Path = DEFAULT_STATE_PATH) -> None:
    """Raises CooldownActive if `source` was last run within `cooldown_seconds`.
    Otherwise (or if force=True) records now as the new last-run time."""
    state = _load(state_path)
    last_run = state.get(source)
    now = time.time()

    if last_run is not None and not force:
        elapsed = now - last_run
        if elapsed < cooldown_seconds:
            raise CooldownActive(source, cooldown_seconds - elapsed)

    state[source] = now
    _save(state_path, state)
