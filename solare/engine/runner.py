"""Drives the real encode pipeline for a whole queue, one item at a time, in a background thread
so the dashboard's own refresh loop never blocks on a subprocess call (audio transcode/mux can
take real seconds-to-minutes; blocking the UI thread for that isn't acceptable).

Only the video-encode phase is pausable (matching where pausing actually matters - the long-
running phase, not the quick one-shot passes around it). `set_suspended` is re-asserted every
poll while paused, not just once on the transition edge, so a worker that spawns mid-pause gets
caught on the very next check (see Av1anRunner.set_suspended).
"""

from __future__ import annotations

import copy
import datetime
import shutil
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from solare.engine.audio import transcode_audio_track
from solare.engine.av1an import Av1anRunner
from solare.engine.chunk_progress import ChunkProgress
from solare.engine.config import TitleConfig
from solare.engine.dolby_vision import inject_rpu
from solare.engine.integrity import check_output_integrity
from solare.engine.mux import mux_episode, resolve_subtitle_sources
from solare.engine.queue import QueueItem, build_queue

_POLL_INTERVAL_SECONDS = 1.0


class RunPhase(Enum):
    VIDEO_ENCODE = "video encoding"
    DOLBY_VISION = "Dolby Vision injection"
    AUDIO = "audio transcoding"
    MUX = "muxing"
    INTEGRITY = "verifying output"
    DONE = "done"
    FAILED = "failed"


@dataclass
class RunState:
    phase: RunPhase = RunPhase.VIDEO_ENCODE
    item_index: int = 0
    item_count: int = 0
    current_item_name: str = ""
    chunks_done: int = 0
    chunks_total: int = 0
    chunk_progress: ChunkProgress | None = None
    paused: bool = False
    error: str | None = None
    log_lines: list[str] = field(default_factory=list)
    started_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    item_started_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    item_paused_seconds: float = 0.0


class JobRunner:
    def __init__(self, config: TitleConfig):
        self._config = config
        self._queue: list[QueueItem] = build_queue(config)
        self._lock = threading.Lock()
        self._state = RunState(item_count=len(self._queue))
        self._paused = threading.Event()
        self._stop = threading.Event()
        self._av1an: Av1anRunner | None = None
        self._pause_started_at: datetime.datetime | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def pause(self) -> None:
        self._paused.set()
        with self._lock:
            self._state.paused = True
            self._pause_started_at = datetime.datetime.now()

    def resume(self) -> None:
        self._paused.clear()
        with self._lock:
            self._state.paused = False
            if self._pause_started_at is not None:
                elapsed = (datetime.datetime.now() - self._pause_started_at).total_seconds()
                self._state.item_paused_seconds += elapsed
                self._pause_started_at = None

    def stop(self) -> None:
        self._stop.set()
        if self._av1an is not None:
            self._av1an.terminate()

    def get_state(self) -> RunState:
        with self._lock:
            return copy.deepcopy(self._state)

    def _log(self, message: str) -> None:
        with self._lock:
            self._state.log_lines.append(message)
            self._state.log_lines = self._state.log_lines[-200:]

    def _run(self) -> None:
        for i, item in enumerate(self._queue):
            if self._stop.is_set():
                return
            with self._lock:
                self._state.item_index = i + 1
                self._state.current_item_name = item.src_file.name
                self._state.chunks_done = 0
                self._state.chunks_total = 0
                self._state.chunk_progress = None
                self._state.item_started_at = datetime.datetime.now()
                self._state.item_paused_seconds = 0.0
            if item.already_done:
                self._log(f"skipping {item.src_file.name} - output already exists")
                continue
            try:
                self._run_item(item)
            except Exception as e:  # noqa: BLE001 - surfaced to the dashboard, not swallowed
                with self._lock:
                    self._state.phase = RunPhase.FAILED
                    self._state.error = str(e)
                self._log(f"FAILED: {item.src_file.name}: {e}")
                return
        with self._lock:
            self._state.phase = RunPhase.DONE

    def _run_item(self, item: QueueItem) -> None:
        temp_dir_override = self._config.video.temp_dir
        temp_dir = (
            Path(temp_dir_override)
            if temp_dir_override
            else item.out_file.parent / f"{item.out_file.stem}.av1an-temp"
        )
        video_tmp = item.out_file.parent / f"{item.out_file.stem}.video.tmp.mkv"

        with self._lock:
            self._state.phase = RunPhase.VIDEO_ENCODE
        self._av1an = Av1anRunner(
            self._config, item.src_file, video_tmp, temp_dir, chunk_method=self._config.video.chunk_method
        )
        self._av1an.start()
        self._log(f"video encode started: {item.src_file.name}")

        exit_code = self._wait_for_av1an()
        self._av1an = None
        with self._lock:
            self._state.chunk_progress = None
        if self._stop.is_set():
            return
        if exit_code != 0:
            raise RuntimeError(f"av1an exited with code {exit_code}")
        self._log("video encode finished")

        current_video = video_tmp
        if self._config.video.dovi_rpu:
            with self._lock:
                self._state.phase = RunPhase.DOLBY_VISION
            self._log("injecting Dolby Vision RPU")
            dv_out = item.out_file.parent / f"{item.out_file.stem}.video.dv.mkv"
            inject_rpu(current_video, Path(self._config.video.dovi_rpu), dv_out, temp_dir)
            current_video.unlink(missing_ok=True)
            current_video = dv_out

        with self._lock:
            self._state.phase = RunPhase.AUDIO
        audio_files = []
        for t, track in enumerate(self._config.audio_tracks):
            self._log(f"transcoding audio track {t}: {track.title}")
            audio_out = item.out_file.parent / f"{item.out_file.stem}.audio{t}.tmp.mka"
            transcode_audio_track(
                item.src_file, track, self._config.audio_source_language, audio_out
            )
            audio_files.append(audio_out)

        with self._lock:
            self._state.phase = RunPhase.MUX
        self._log("muxing")
        sub_sources = resolve_subtitle_sources(self._config, item.src_file, episode_tag=None)
        mux_episode(self._config, item.src_file, current_video, audio_files, sub_sources, item.out_file)

        current_video.unlink(missing_ok=True)
        for audio_file in audio_files:
            audio_file.unlink(missing_ok=True)
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

        with self._lock:
            self._state.phase = RunPhase.INTEGRITY
        result = check_output_integrity(
            item.src_file, item.out_file, len(self._config.audio_tracks), len(self._config.subtitles)
        )
        if not result.ok:
            item.out_file.rename(item.out_file.with_suffix(item.out_file.suffix + ".FAILED"))
            raise RuntimeError(f"integrity check failed: {result.reason}")
        self._log(f"integrity OK: {result.reason}")

    def _wait_for_av1an(self) -> int | None:
        while True:
            if self._stop.is_set():
                self._av1an.terminate()
                return None
            self._av1an.set_suspended(self._paused.is_set())
            progress = self._av1an.get_progress()
            chunk_progress = self._av1an.get_chunk_progress()
            with self._lock:
                if progress is not None:
                    self._state.chunks_done = progress.done_frames
                    self._state.chunks_total = progress.total_frames
                self._state.chunk_progress = chunk_progress
            exit_code = self._av1an.poll()
            if exit_code is not None:
                return exit_code
            time.sleep(_POLL_INTERVAL_SECONDS)
