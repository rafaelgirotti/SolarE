"""Background Growatt polling - the API isn't meant to be hit on every check, so this runs on its
own thread on a fixed interval, and callers just read whatever the last successful poll produced
(same lock-guarded-snapshot pattern as engine.JobRunner). Used by both the dashboard's solar panel
and engine.runner's solar-gated auto-pause - genuinely independent of either, hence living here
rather than under tui/.
"""

from __future__ import annotations

import datetime
import queue
import threading

from solare.solar import cache
from solare.solar.client import GenerationSummary, GrowattClient, GrowattCredentials

POLL_INTERVAL_SECONDS = 60.0
# growattServer's requests.Session (base_api.py) never passes timeout= on any call - confirmed by
# reading its source, not assumed. Python's requests has no default timeout, so a network hiccup
# that accepts the connection but never responds hangs the call forever. Confirmed live: a real
# poll got stuck this way and stayed stale for hours, with the app only recovering after being
# killed and restarted - the polling loop was blocked inside the try, never reaching the except,
# never reaching the next scheduled retry. This is the ceiling on how long one poll attempt can
# block before being treated as failed - generous over a normal few-second round trip, comfortably
# under POLL_INTERVAL_SECONDS so a timed-out attempt doesn't run into the next scheduled one.
POLL_TIMEOUT_SECONDS = 30.0
# How old a reading (disk-cached or from an earlier live poll) can be and still be trusted for a
# gating decision - past this, is_producing() reports "unknown" (None) rather than confidently
# reusing a number that may no longer reflect reality. 10 minutes: long enough to bridge a
# restart landing mid-outage or a few consecutive missed polls, short enough that it's still a
# real, recent reading of actual conditions, not a guess.
MAX_READING_AGE_SECONDS = 600.0


def _poll_with_timeout(client: GrowattClient, timeout: float) -> GenerationSummary:
    """Runs the real API call on its own throwaway daemon thread and waits up to `timeout` for
    it, rather than calling it directly on the polling loop's own thread. A fresh thread per
    attempt (not a reused worker pool) matters: if this attempt's call is the one that hangs,
    giving up on it after `timeout` must not block the *next* scheduled attempt from getting its
    own genuinely fresh try. The abandoned thread just keeps running harmlessly in the background
    (a leaked daemon thread, nothing waits on it) until it eventually resolves or the process
    exits - there's no way to forcibly cancel an in-flight requests call from outside it."""
    result: queue.Queue = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result.put(("ok", client.get_generation_summary()))
        except Exception as e:  # noqa: BLE001 - forwarded to the caller via the queue, not swallowed
            result.put(("error", e))

    threading.Thread(target=worker, daemon=True, name="solar-poll-attempt").start()
    try:
        status, value = result.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError(f"Growatt poll timed out after {timeout:.0f}s") from None
    if status == "error":
        raise value
    return value


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
                summary = _poll_with_timeout(self._client, POLL_TIMEOUT_SECONDS)
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
