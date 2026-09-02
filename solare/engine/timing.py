"""Persists each completed queue item's real encode duration next to its output, so the
whole-batch ETA survives a restart.

A batch can run for days with solare stopped and restarted many times along the way (see
runner.RunState.completed_item_seconds) - an in-memory-only average resets to nothing on every
restart, at which point it stays empty until a fresh item finishes under that same process, which
for a long-running batch can be most of a day away. Keyed by each item's own resolved output path
(already the same identity QueueItem.already_done checks against) rather than the source
filename - stable and unique even across multiple folder_entry.out subfolders under one title's
output root.
"""

from __future__ import annotations

import json
from pathlib import Path

_FILENAME = ".solare-item-durations.json"


def _path(output_root: Path) -> Path:
    return output_root / _FILENAME


def load(output_root: Path) -> dict[str, float]:
    path = _path(output_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def record(output_root: Path, item_key: str, seconds: float) -> None:
    durations = load(output_root)
    durations[item_key] = seconds
    _path(output_root).write_text(json.dumps(durations, indent=2), encoding="utf-8")
