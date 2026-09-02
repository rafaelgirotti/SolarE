"""Background Growatt polling - the API isn't meant to be hit on every check, so this runs on its
own thread on a fixed interval, and callers just read whatever the last successful poll produced
(same lock-guarded-snapshot pattern as engine.JobRunner). Used by both the dashboard's solar panel
and engine.runner's solar-gated auto-pause - genuinely independent of either, hence living here
rather than under tui/.
"""

from __future__ import annotations

import datetime
import threading

from solare.solar import cache
from solare.solar.client import GenerationSummary, GrowattClient, GrowattCredentials

POLL_INTERVAL_SECONDS = 60.0
# How old a reading (disk-cached or from an earlier live poll) can be and still be trusted for a
# gating decision - past this, is_producing() reports "unknown" (None) rather than confidently
# reusing a number that may no longer reflect reality. 10 minutes: long enough to bridge a
# restart landing mid-outage or a few consecutive missed polls, short enough that it's still a
# real, recent reading of actual conditions, not a guess.
MAX_READING_AGE_SECONDS = 600.0


class SolarPoller:
    def __init__(self, credentials: GrowattCredentials):
        self._client = GrowattClient(credentials)
        self._lock = threading.Lock()
        self._summary: GenerationSummary | None = None
        self._checked_at: datetime.datetime | None = None
        self._error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        cached = cache.load()
        if cached is not None:
            self._summary, self._checked_at = cached

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                summary = self._client.get_generation_summary()
                checked_at = datetime.datetime.now()
                with self._lock:
                    self._summary = summary
                    self._checked_at = checked_at
                    self._error = None
                cache.save(summary, checked_at)
            except Exception as e:  # noqa: BLE001 - surfaced to callers, not swallowed
                with self._lock:
                    self._error = str(e)
            self._stop.wait(POLL_INTERVAL_SECONDS)

    def get_latest(self) -> tuple[GenerationSummary | None, datetime.datetime | None, str | None]:
        with self._lock:
            return self._summary, self._checked_at, self._error

    def is_producing(self, min_watts: float) -> bool | None:
        """None means "no data to judge by yet" - distinct from False, so a gate can choose to
        fail open (don't block on missing data) rather than treating it as "not producing". Also
        None once the last known reading (disk-cached or from an earlier live poll) is older than
        MAX_READING_AGE_SECONDS - an old reading confidently reused forever regardless of how long
        the API's been unreachable is worse than admitting it's unknown, same reasoning as the
        missing-data case."""
        summary, checked_at, _ = self.get_latest()
        if summary is None or checked_at is None:
            return None
        age_seconds = (datetime.datetime.now() - checked_at).total_seconds()
        if age_seconds > MAX_READING_AGE_SECONDS:
            return None
        return summary.current_power_w >= min_watts
