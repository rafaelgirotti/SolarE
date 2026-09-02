"""Persists the last successful Growatt reading to disk, so a fresh app launch has something to
judge freshness against immediately.

Without this, a real API gap landing exactly at startup (before the first live poll succeeds)
left SolarPoller with nothing but None - and the pre-start solar gate (JobRunner.
_wait_for_solar_gate_before_start) requires an explicit "producing" reading before it'll ever let
av1an start, so it just sits there waiting even though production was almost certainly fine
moments before the restart. This only bridges that gap - SolarPoller.is_producing() still checks
the reading's age (whether it came from here or a live poll) before trusting it, so a genuinely
stale value (a real multi-hour outage, or restarting hours later) still falls back to "unknown."

Gitignored like credentials.json/state.json - real account usage data, not something to commit.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import asdict
from pathlib import Path

from solare.solar.client import GenerationSummary

_CACHE_PATH = Path(__file__).resolve().parents[2] / "solar-cache.json"


def load() -> tuple[GenerationSummary, datetime.datetime] | None:
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        checked_at = datetime.datetime.fromisoformat(data["checked_at"])
        summary = GenerationSummary(**data["summary"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None
    return summary, checked_at


def save(summary: GenerationSummary, checked_at: datetime.datetime) -> None:
    try:
        _CACHE_PATH.write_text(
            json.dumps({"summary": asdict(summary), "checked_at": checked_at.isoformat()}),
            encoding="utf-8",
        )
    except OSError:
        pass  # best-effort - losing this only means the next launch starts with no head start
