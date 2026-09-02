"""The data one dashboard render needs.

Kept deliberately separate from where the data comes from - `app.py`'s rendering code doesn't
need to know whether a `JobState`/`SolarState` came from a live poll or (in tests) a fake one.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass


@dataclass
class ActiveChunkInfo:
    """One worker's currently-in-progress chunk, from av1an's own log (see
    engine.chunk_progress) - done.json alone can't say this, only completed chunks."""

    worker_id: int
    chunk_index: str
    elapsed_seconds: float
    avg_seconds: float | None
    stuck: bool


@dataclass
class JobState:
    title: str
    phase: str
    item_index: int
    item_count: int
    frames_done: int
    frames_total: int
    settings_summary: str
    config_path: str
    eta_text: str
    batch_summary: str | None
    batch_eta_text: str | None  # whole-batch ETA, from the average of already-completed items'
    # real encode time - see live_job._batch_eta_text. None until at least one item has finished.
    current_item_name: str | None  # display name of the item currently being processed - kept
    current_item_src_path: str | None  # separate from batch_summary so the dashboard can render
    # it as its own non-wrapping, ellipsis-truncating hyperlink to the real source file
    output_path: str
    disk_free_gb: float
    output_used_gb: float
    started_at: datetime.datetime
    active_chunks: list[ActiveChunkInfo]
    waiting_for_solar: bool
    solar_override: bool
    overall_pct: float  # spans every phase (video encode + audio + mux + integrity), not just
    # frames_done/frames_total - see live_job._overall_pct


@dataclass
class SolarState:
    """Only fields the Growatt API actually provides for a meter-less tlx account (see
    docs/growatt-api.md) - no weather, per-MPPT PV strings, or AC voltage/frequency, none of which
    `plant_energy_data` exposes."""

    line: str
    ok: bool
    today_kwh: float
    month_kwh: float
    total_kwh: float
    capacity_pct: float
    nominal_power_w: float
    stale: bool = False  # last poll failed, showing a previously-cached reading instead
