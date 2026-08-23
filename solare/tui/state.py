"""The data one dashboard render needs.

Kept deliberately separate from where the data comes from - the mock source (`mock.py`) and the
real `solare.engine` job runner (once it exists) both just need to produce one of these each tick;
`app.py`'s rendering code doesn't change either way.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from solare.hwmonitor import HardwareSnapshot


@dataclass
class JobState:
    title: str
    phase: str
    item_index: int
    item_count: int
    job_name: str
    chunks_done: int
    chunks_total: int
    settings_summary: str
    eta_item: str
    eta_batch: str | None
    disk_free_gb: float
    disk_drive: str
    solar_line: str
    solar_ok: bool
    started_at: datetime.datetime


@dataclass
class DashboardState:
    job: JobState
    hw: HardwareSnapshot
    log_lines: list[str] = field(default_factory=list)
