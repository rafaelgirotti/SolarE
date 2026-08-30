"""Background Growatt polling - the API isn't meant to be hit on every check, so this runs on its
own thread on a fixed interval, and callers just read whatever the last successful poll produced
(same lock-guarded-snapshot pattern as engine.JobRunner). Used by both the dashboard's solar panel
and engine.runner's solar-gated auto-pause - genuinely independent of either, hence living here
rather than under tui/.
"""

from __future__ import annotations

import datetime
import threading

from solare.solar.client import GenerationSummary, GrowattClient, GrowattCredentials

POLL_INTERVAL_SECONDS = 60.0


class SolarPoller:
    def __init__(self, credentials: GrowattCredentials):
        self._client = GrowattClient(credentials)
        self._lock = threading.Lock()
        self._summary: GenerationSummary | None = None
        self._checked_at: datetime.datetime | None = None
        self._error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                summary = self._client.get_generation_summary()
                with self._lock:
                    self._summary = summary
                    self._checked_at = datetime.datetime.now()
                    self._error = None
            except Exception as e:  # noqa: BLE001 - surfaced to callers, not swallowed
                with self._lock:
                    self._error = str(e)
            self._stop.wait(POLL_INTERVAL_SECONDS)

    def get_latest(self) -> tuple[GenerationSummary | None, datetime.datetime | None, str | None]:
        with self._lock:
            return self._summary, self._checked_at, self._error

    def is_producing(self, min_watts: float) -> bool | None:
        """None means "no data to judge by yet" - distinct from False, so a gate can choose to
        fail open (don't block on missing data) rather than treating it as "not producing"."""
        summary, _, _ = self.get_latest()
        if summary is None:
            return None
        return summary.current_power_w >= min_watts
