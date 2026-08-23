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
    chunks_done: int
    chunks_total: int
    settings_summary: str
    config_path: str
    eta_text: str
    batch_summary: str | None
    output_path: str
    disk_free_gb: float
    output_used_gb: float
    started_at: datetime.datetime


@dataclass
class SolarState:
    line: str
    ok: bool
    today_kwh: float
    month_kwh: float
    total_kwh: float
    capacity_pct: float
    weather_temp_c: float
    weather_condition: str
    pv_strings: list[tuple[float, float]]  # (volts, amps) per active MPPT string
    ac_voltage: float
    ac_frequency: float


@dataclass
class DashboardState:
    job: JobState
    hw: HardwareSnapshot
    solar: SolarState
    log_lines: list[str] = field(default_factory=list)
