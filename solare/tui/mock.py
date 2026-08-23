"""Fake encode-job progress for the Phase 3 dashboard shell.

Title/settings/paths come from a real loaded `TitleConfig` - only progress-over-time (chunks,
ETA, log lines) is simulated here, standing in for what `solare.engine`'s real job runner will
produce. Hardware stats and solar data are NOT handled here - see `app.py`, which polls
`solare.hwmonitor` directly (works even with no job loaded) and `mock_solar_state()` below.
"""

from __future__ import annotations

import datetime
import random
import shutil
from pathlib import Path

from solare.engine import TitleConfig
from solare.tui.state import JobState, SolarState

_MOCK_AVG_ITEM_SIZE_GB = 0.85
_MOCK_NOMINAL_POWER_W = 7500.0  # plant_energy_data's nominalPowerStr, confirmed field

_FAKE_LOG_MESSAGES = [
    "chunk finished ({frames} frames, {secs:.1f}s)",
    "chunk started",
    "worker {worker}: encoding at {fps:.1f} fps",
]


def mock_solar_state() -> SolarState:
    """Real field shapes (see docs/solar-api.md), not polled live - Growatt's API isn't meant to
    be hit every second, so real wiring waits for a background poll loop separate from the
    dashboard's own refresh tick."""
    current_power_w = 2255.4
    return SolarState(
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


class MockJobSource:
    def __init__(
        self,
        config: TitleConfig,
        chunks_total: int = 1400,
        item_index: int = 1,
        item_count: int = 1,
    ) -> None:
        self._config = config
        self._chunks_total = chunks_total
        self._item_index = item_index
        self._item_count = item_count
        self.reset()

    def reset(self) -> None:
        self._started_at = datetime.datetime.now()
        self._chunks_done = 0
        self._log_lines: list[str] = []
        self._tick = 0
        self._paused = False

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def log_lines(self) -> list[str]:
        return list(self._log_lines)

    def poll_job(self) -> JobState:
        self._tick += 1
        if not self._paused and self._tick % 2 == 0 and self._chunks_done < self._chunks_total:
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
        eta_text = "paused" if self._paused else f"{eta_time.strftime('%H:%M')} (in 4h 15m)"

        batch_summary = None
        if self._item_count > 1:
            completed = self._item_index - 1
            batch_eta = now + datetime.timedelta(hours=38, minutes=10)
            batch_summary = (
                f"{self._item_index}/{self._item_count} items - "
                f"ETA {batch_eta.strftime('%Y-%m-%d %H:%M')} "
                f"(avg 14.2h/item, {completed} completed)"
            )

        # config.output_root is a placeholder in config.example.json ("/path/to/..."), which
        # doesn't exist on disk - fall back to the config file's own directory so disk_usage has
        # somewhere real to check. A real per-title config would point at a real output directory.
        output_path = Path(self._config.output_root)
        if not output_path.exists():
            output_path = self._config.path.parent
        disk_usage = shutil.disk_usage(output_path)
        completed_items = max(0, self._item_index - 1)

        return JobState(
            title=self._config.title,
            phase="video encoding",
            item_index=self._item_index,
            item_count=self._item_count,
            chunks_done=self._chunks_done,
            chunks_total=self._chunks_total,
            settings_summary=self._config.settings_summary,
            config_path=str(self._config.path.resolve()),
            eta_text=eta_text,
            batch_summary=batch_summary,
            output_path=str(output_path),
            disk_free_gb=round(disk_usage.free / (1024**3), 1),
            output_used_gb=round(completed_items * _MOCK_AVG_ITEM_SIZE_GB, 2),
            started_at=self._started_at,
        )
