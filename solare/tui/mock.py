"""Fake encode-job progress for the Phase 3 dashboard shell.

Hardware stats are real, read live from this machine via `solare.hwmonitor`. Only the encode-job
fields (chunks, ETA, log lines) are simulated here, standing in for what `solare.engine` will
produce once the real orchestration engine exists.
"""

from __future__ import annotations

import datetime
import random

from solare.hwmonitor import HardwareMonitor
from solare.tui.state import DashboardState, JobState

_FAKE_LOG_MESSAGES = [
    "chunk finished ({frames} frames, {secs:.1f}s)",
    "chunk started",
    "worker {worker}: encoding at {fps:.1f} fps",
]


class MockDataSource:
    def __init__(self, chunks_total: int = 1400) -> None:
        self._hw_monitor = HardwareMonitor()
        self._started_at = datetime.datetime.now()
        self._chunks_done = 26
        self._chunks_total = chunks_total
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

        pct_done = 100.0 * self._chunks_done / self._chunks_total

        job = JobState(
            title="Example Title (2026)",
            phase="video encoding",
            item_index=1,
            item_count=1,
            job_name="x265 video encoding",
            chunks_done=self._chunks_done,
            chunks_total=self._chunks_total,
            settings_summary="x265 slow preset, CRF22 - config: config.example.json",
            eta_item=f"{pct_done:.1f}% done",
            eta_batch=None,
            disk_free_gb=812.4,
            disk_drive="D:",
            solar_line="2255.4 W (checked 0.3m ago)",
            solar_ok=True,
            started_at=self._started_at,
        )
        return DashboardState(job=job, hw=self._hw_monitor.poll(), log_lines=list(self._log_lines))

    def close(self) -> None:
        self._hw_monitor.close()
