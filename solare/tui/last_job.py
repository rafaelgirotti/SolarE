"""Tracks the last config a real job was started with (see app.py's action_start), so reopening
solare with no --config argument can offer to resume it instead of starting from a blank picker
every time - the whole point of solar-gated, resumable encoding is surviving exactly this kind of
interruption (closing the terminal, a reboot, a crash) without losing track of what was running.

Gitignored like credentials.json - the last config's path is a real, personal filesystem detail,
not something to ever commit.
"""

from __future__ import annotations

import json
from pathlib import Path

_STATE_PATH = Path(__file__).resolve().parents[2] / "state.json"


def record_last_config(config_path: Path) -> None:
    try:
        _STATE_PATH.write_text(json.dumps({"last_config": str(config_path.resolve())}))
    except OSError:
        pass  # best-effort - losing this only skips next launch's resume prompt, nothing worse


def last_config_path() -> Path | None:
    try:
        data = json.loads(_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    raw = data.get("last_config")
    return Path(raw) if raw else None
