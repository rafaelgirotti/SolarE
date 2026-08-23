"""Fake encode-job progress for the Phase 3 dashboard shell.

Hardware stats are real, read live from this machine via `solare.hwmonitor`. Encode-job fields
(chunks, ETA, log lines) and solar generation are simulated - the values used for solar match the
real shape confirmed against a live account (see docs/solar-api.md and
GrowattClient.get_generation_summary()), but aren't polled live here: Growatt's API does a full
login per call and isn't meant to be hit every second, so real wiring waits until there's a
background poll loop separate from the dashboard's own refresh tick.
"""

from __future__ import annotations

import datetime
import random
import shutil
from pathlib import Path

from solare.hwmonitor import HardwareMonitor
from solare.tui.state import DashboardState, JobState, SolarState

# Stand-in for a real per-title config's output.root - there's no solare.engine to read one from
# yet. Free-space is a genuine shutil.disk_usage() call against this path, not simulated; only the
# path itself (which directory a real job would be writing to) is a placeholder.
_MOCK_OUTPUT_PATH = Path.cwd()
_MOCK_AVG_ITEM_SIZE_GB = 0.85
_MOCK_NOMINAL_POWER_W = 7500.0  # plant_energy_data's nominalPowerStr, confirmed field

_FAKE_LOG_MESSAGES = [
    "chunk finished ({frames} frames, {secs:.1f}s)",
    "chunk started",
    "worker {worker}: encoding at {fps:.1f} fps",
]


class MockDataSource:
    def __init__(
        self,
        chunks_total: int = 1400,
        item_index: int = 1,
        item_count: int = 1,
    ) -> None:
        self._hw_monitor = HardwareMonitor()
        self._started_at = datetime.datetime.now()
        self._chunks_done = 26
        self._chunks_total = chunks_total
        self._item_index = item_index
        self._item_count = item_count
        self._log_lines: list[str] = []
        self._tick = 0

    def poll(self) -> DashboardState:
        self._tick += 1
        if self._tick % 2 == 0 and self._chunks_done < self._chunks_total:
            self._chunks_done += 1
            template = random.choice(_FAKE_LOG_MESSAGES)
            message = template.format(
                frames=random.randint(280, 310),
                secs=random.uniform(3.5, 5.0),
                worker=random.randint(1, 4),
                fps=random.uniform(60.0, 85.0),
            )
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            self._log_lines.append(f"{timestamp}  chunk {self._chunks_done:04d}: {message}")
            self._log_lines = self._log_lines[-200:]

        now = datetime.datetime.now()
        eta_time = now + datetime.timedelta(hours=4, minutes=15)
        eta_text = f"{eta_time.strftime('%H:%M')} (in 4h 15m)"

        batch_summary = None
        if self._item_count > 1:
            completed = self._item_index - 1
            batch_eta = now + datetime.timedelta(hours=38, minutes=10)
            batch_summary = (
                f"{self._item_index}/{self._item_count} items - "
                f"ETA {batch_eta.strftime('%Y-%m-%d %H:%M')} "
                f"(avg 14.2h/item, {completed} completed)"
            )

        disk_usage = shutil.disk_usage(_MOCK_OUTPUT_PATH)
        completed_items = max(0, self._item_index - 1)

        job = JobState(
            title="Example Title (2026)",
            phase="video encoding",
            item_index=self._item_index,
            item_count=self._item_count,
            chunks_done=self._chunks_done,
            chunks_total=self._chunks_total,
            settings_summary="x265 slow preset, CRF22",
            config_path=str((Path(__file__).parents[2] / "config" / "config.example.json").resolve()),
            eta_text=eta_text,
            batch_summary=batch_summary,
            output_path=str(_MOCK_OUTPUT_PATH),
            disk_free_gb=round(disk_usage.free / (1024**3), 1),
            output_used_gb=round(completed_items * _MOCK_AVG_ITEM_SIZE_GB, 2),
            started_at=self._started_at,
        )
        current_power_w = 2255.4
        solar = SolarState(
            line=f"{current_power_w} W (checked 0.3m ago)",
            ok=True,
            today_kwh=20.8,
            month_kwh=344.9,
            total_kwh=48514.0,
            capacity_pct=round(100.0 * current_power_w / _MOCK_NOMINAL_POWER_W, 1),
            weather_temp_c=12.0,
            weather_condition="Cloudy",
            pv_strings=[(327.5, 6.3), (331.8, 6.3)],
            ac_voltage=223.7,
            ac_frequency=60.0,
        )
        return DashboardState(
            job=job, hw=self._hw_monitor.poll(), solar=solar, log_lines=list(self._log_lines)
        )

    def close(self) -> None:
        self._hw_monitor.close()
