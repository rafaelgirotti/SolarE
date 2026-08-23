"""Parses av1an's own DEBUG-level 'started chunk'/'finished chunk' broker log lines to track
per-worker chunk timing.

`done.json` alone can't answer "is a chunk stuck" - it only records completed chunks (frames,
size_bytes), never when one started. av1an's own log does, at DEBUG level, unconditionally (no
`-v`/verbosity flag needed - confirmed directly against a real run's log), with a duration already
computed per finished chunk ("took 91.28s").
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path

# Real example lines this matches:
#   2026-08-23T22:08:52.710017Z DEBUG encode_chunk{worker_id=1 total_chunks=822 chunk_index="00168"}: av1an_core::broker:  started chunk 00168: 140 frames
#   2026-08-23T22:10:23.990176Z DEBUG encode_chunk{worker_id=0 total_chunks=822 chunk_index="00424"}: av1an_core::broker: finished chunk 00424: 140 frames, 1.53 fps, took 91.28s
_STARTED_RE = re.compile(
    r'^(?P<ts>\S+) DEBUG encode_chunk\{worker_id=(?P<worker>\d+)[^}]*chunk_index="(?P<chunk>\d+)"\}: '
    r"av1an_core::broker:\s+started chunk \d+: (?P<frames>\d+) frames$"
)
_FINISHED_RE = re.compile(
    r'^(?P<ts>\S+) DEBUG encode_chunk\{worker_id=(?P<worker>\d+)[^}]*chunk_index="(?P<chunk>\d+)"\}: '
    r"av1an_core::broker: finished chunk \d+: \d+ frames, [\d.]+ fps, took (?P<secs>[\d.]+)s$"
)

_TAIL_BYTES = 1_000_000  # generous: at ~150 bytes/line, still covers hours of history per worker
_STUCK_MULTIPLIER = 3.0
_STUCK_FLOOR_SECONDS = 300.0  # grace period before flagging anything, even with a fast average


@dataclass
class ActiveChunk:
    worker_id: int
    chunk_index: str
    frames: int
    started_at: datetime.datetime

    def elapsed_seconds(self, now: datetime.datetime | None = None) -> float:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        return (now - self.started_at).total_seconds()


@dataclass
class ChunkProgress:
    active: list[ActiveChunk]
    recent_durations_seconds: list[float]

    @property
    def avg_duration_seconds(self) -> float | None:
        if not self.recent_durations_seconds:
            return None
        return sum(self.recent_durations_seconds) / len(self.recent_durations_seconds)

    def is_stuck(self, chunk: ActiveChunk, now: datetime.datetime | None = None) -> bool:
        """Heuristic, not certainty: flags a chunk running far longer than its recent peers, with
        a flat grace floor so a short recent average doesn't make this hair-trigger."""
        avg = self.avg_duration_seconds
        if avg is None:
            return False
        return chunk.elapsed_seconds(now) > max(avg * _STUCK_MULTIPLIER, _STUCK_FLOOR_SECONDS)


def find_latest_log(logs_dir: Path) -> Path | None:
    """av1an names its log av1an.log.<date> - pick the most recently modified match, since a
    resumed run spanning midnight would start a new one."""
    candidates = sorted(logs_dir.glob("av1an.log.*"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def parse_chunk_progress(log_path: Path, recent_count: int = 10) -> ChunkProgress:
    """Only tails the last _TAIL_BYTES of the log, not the whole file - av1an's log grows
    unboundedly over a many-hour encode, and only recent lines matter here. A line split by the
    seek offset is simply dropped by splitlines() (matches neither regex), which is fine."""
    size = log_path.stat().st_size
    with log_path.open("rb") as f:
        if size > _TAIL_BYTES:
            f.seek(size - _TAIL_BYTES)
        raw = f.read()

    active: dict[int, ActiveChunk] = {}
    durations: list[float] = []

    for line in raw.decode("utf-8", errors="replace").splitlines():
        m = _FINISHED_RE.match(line)
        if m:
            active.pop(int(m.group("worker")), None)
            durations.append(float(m.group("secs")))
            continue
        m = _STARTED_RE.match(line)
        if m:
            worker = int(m.group("worker"))
            active[worker] = ActiveChunk(
                worker_id=worker,
                chunk_index=m.group("chunk"),
                frames=int(m.group("frames")),
                started_at=datetime.datetime.fromisoformat(m.group("ts")),
            )

    return ChunkProgress(
        active=sorted(active.values(), key=lambda c: c.worker_id),
        recent_durations_seconds=durations[-recent_count:],
    )
