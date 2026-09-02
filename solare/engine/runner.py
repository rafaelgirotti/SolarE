"""Drives the real encode pipeline for a whole queue, one item at a time, in a background thread
so the dashboard's own refresh loop never blocks on a subprocess call (audio transcode/mux can
take real seconds-to-minutes; blocking the UI thread for that isn't acceptable).

Only the video-encode phase is pausable (matching where pausing actually matters - the long-
running phase, not the quick one-shot passes around it). `set_suspended` is re-asserted every
poll while paused, not just once on the transition edge, so a worker that spawns mid-pause gets
caught on the very next check (see Av1anRunner.set_suspended).

A config's `solarGate` composes with manual pause rather than replacing it: the effective suspend
state is manual-pause OR solar-gated, so solar coming back never auto-resumes a job the user
paused themselves, and pausing manually during a solar gate doesn't get silently overridden either
way once the sun does the same. Missing solar data (poller not yet polled, or no poller at all)
fails open - never gates on absence of information.
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
from solare.engine import timing
from solare.solar import SolarPoller

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
    frames_done: int = 0  # av1an's done.json is frame-based, not chunk-based - see
    frames_total: int = 0  # ChunkProgress/ActiveChunkInfo for real per-chunk tracking
    chunk_progress: ChunkProgress | None = None
    paused: bool = False  # user-requested, via pause()
    solar_paused: bool = False  # auto, via solarGate - independent of the above, see module docstring
    # Set whenever a pause window (manual or solar-gated) is currently open, cleared when it
    # closes - lets a UI freeze a chunk's displayed elapsed time instead of it ticking through a
    # suspend it isn't actually progressing through (item_paused_seconds alone only captures
    # *closed* windows, not time elapsed in the one currently open).
    pause_started_at: datetime.datetime | None = None
    waiting_for_solar: bool = False  # blocking *before* av1an starts at all - distinct from
    # solar_paused (which suspends an av1an already running); see JobRunner._wait_for_solar_gate
    solar_override: bool = False  # user toggled solar gating off for the rest of this run
    finalizing: bool = False  # every chunk done, av1an still running its own mkvmerge concat -
    # see JobRunner._wait_for_av1an
    audio_track_index: int = 0  # 1-based "currently transcoding track N of M" - the only
    audio_track_count: int = 0  # sub-progress available within the AUDIO phase (no per-track
    # frame/byte-level progress tracking exists), used to compute the overall cross-phase percentage
    error: str | None = None
    log_lines: list[str] = field(default_factory=list)
    started_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    item_started_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    item_paused_seconds: float = 0.0
    # Active (paused-time-excluded) wall-clock seconds for each item actually encoded this run -
    # not skipped ones (already_done items take ~0 real time and would skew the average sharply
    # downward). Batch ETA is average-of-these times remaining items - see live_job._batch_eta_text.
    completed_item_seconds: list[float] = field(default_factory=list)


class JobRunner:
    def __init__(self, config: TitleConfig, solar_poller: SolarPoller | None = None):
        self._config = config
        self._queue: list[QueueItem] = build_queue(config)
        self._lock = threading.Lock()
        self._state = RunState(item_count=len(self._queue))
        # Seed the batch-ETA average with real durations from items already completed in a
        # previous run of this same batch (persisted across restarts - see engine.timing) - only
        # ones this queue currently sees as already_done, so a stale entry for a title whose
        # source file no longer matches doesn't sneak into the average.
        durations = timing.load(Path(self._config.output_root))
        for item in self._queue:
            if item.already_done:
                seconds = durations.get(str(item.out_file))
                if seconds is not None:
                    self._state.completed_item_seconds.append(seconds)
        self._paused = threading.Event()
        self._solar_gated = threading.Event()
        self._solar_override = threading.Event()  # user chose to skip solar gating for this run
        self._stop = threading.Event()
        self._av1an: Av1anRunner | None = None
        self._solar_poller = solar_poller
        self._pause_started_at: datetime.datetime | None = None
        self._logged_chunk_keys: set[tuple[str, str]] = set()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def is_running(self) -> bool:
        """True until the background thread has actually returned - a real av1an subprocess kill
        (see stop()) is not instant, so a caller that wants to know when a requested stop has
        genuinely taken effect (not just been requested) should poll this rather than assume
        stop() itself was synchronous."""
        return self._thread.is_alive()

    def is_stop_requested(self) -> bool:
        return self._stop.is_set()

    def set_solar_override(self, active: bool) -> None:
        """Toggles solar gating off/back-on for the rest of this run. Turning it back on re-arms
        the ongoing gate checked during video encoding (_update_solar_gate, next poll tick) but
        does not retroactively re-block a pre-start wait that already finished while it was off -
        only the currently-running encode's suspend behavior is affected either way."""
        if active:
            self._solar_override.set()
        else:
            self._solar_override.clear()
        with self._lock:
            self._state.solar_override = active
            if active:
                self._state.waiting_for_solar = False

    def pause(self) -> None:
        self._paused.set()
        with self._lock:
            self._state.paused = True
            if self._pause_started_at is None:
                self._pause_started_at = datetime.datetime.now()
                self._state.pause_started_at = self._pause_started_at

    def resume(self) -> None:
        self._paused.clear()
        with self._lock:
            self._state.paused = False
            self._maybe_close_pause_window_locked()

    def _maybe_close_pause_window_locked(self) -> None:
        """Caller must already hold self._lock. Only actually stops the elapsed-time clock once
        neither pause source (manual or solar-gated) is still active - otherwise a solar gate
        clearing while the user has also manually paused would incorrectly resume ETA accounting."""
        if self._paused.is_set() or self._solar_gated.is_set():
            return
        if self._pause_started_at is not None:
            elapsed = (datetime.datetime.now() - self._pause_started_at).total_seconds()
            self._state.item_paused_seconds += elapsed
            self._pause_started_at = None
            self._state.pause_started_at = None

    def _update_solar_gate(self) -> None:
        gate = self._config.solar_gate
        should_gate = False
        if (
            gate is not None
            and gate.enabled
            and self._solar_poller is not None
            and not self._solar_override.is_set()
        ):
            producing = self._solar_poller.is_producing(gate.min_watts)
            should_gate = producing is False  # None (no data yet) fails open, not gated

        if should_gate == self._solar_gated.is_set():
            return
        if should_gate:
            self._solar_gated.set()
        else:
            self._solar_gated.clear()
        with self._lock:
            self._state.solar_paused = should_gate
            if should_gate and self._pause_started_at is None:
                self._pause_started_at = datetime.datetime.now()
                self._state.pause_started_at = self._pause_started_at
            else:
                self._maybe_close_pause_window_locked()

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

    def _log_newly_finished_chunks(self, chunk_progress: ChunkProgress) -> None:
        """Feeds real per-chunk av1an output into the dashboard's log panel - a curated summary
        of the completed-chunk broker lines already parsed for chunk_progress, not a raw tail of
        av1an's own DEBUG log (which interleaves dozens of near-identical per-worker start/finish
        lines a second and would just be noise). Deduplicated against self._logged_chunk_keys
        since parse_chunk_progress re-parses the same tail window on every poll."""
        for chunk in chunk_progress.recent_finished:
            if chunk.key in self._logged_chunk_keys:
                continue
            self._logged_chunk_keys.add(chunk.key)
            self._log(
                f"chunk {chunk.chunk_index} done - {chunk.frames} frames in "
                f"{chunk.seconds:.1f}s ({chunk.fps:.2f} fps)"
            )

    def _run(self) -> None:
        for i, item in enumerate(self._queue):
            if self._stop.is_set():
                return
            with self._lock:
                self._state.item_index = i + 1
                self._state.current_item_name = item.src_file.name
                self._state.frames_done = 0
                self._state.frames_total = 0
                self._state.chunk_progress = None
                self._state.finalizing = False
                self._state.audio_track_index = 0
                self._state.audio_track_count = 0
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
            if self._stop.is_set():
                # _run_item can return early (mid-item) once stopped rather than raising - without
                # this check, stopping the *last* queued item fell straight through to the DONE
                # branch below, showing a stopped job as finished successfully.
                return
            with self._lock:
                active_seconds = max(
                    0.0,
                    (datetime.datetime.now() - self._state.item_started_at).total_seconds()
                    - self._state.item_paused_seconds,
                )
                self._state.completed_item_seconds.append(active_seconds)
            timing.record(Path(self._config.output_root), str(item.out_file), active_seconds)
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
        self._logged_chunk_keys.clear()

        with self._lock:
            self._state.phase = RunPhase.VIDEO_ENCODE
        # Constructing Av1anRunner is cheap prep work (creates temp_dir, generates the
        # preprocessing script if configured) - safe to do regardless of solar state. Actually
        # starting it is what launches real CPU work (VapourSynth indexing, scene detection, then
        # encoding itself), which is what the wait below gates on.
        self._av1an = Av1anRunner(
            self._config, item.src_file, video_tmp, temp_dir, chunk_method=self._config.video.chunk_method
        )
        self._wait_for_solar_gate_before_start()
        if self._stop.is_set():
            return
        self._av1an.start()
        self._log(f"video encode started: {item.src_file.name}")

        exit_code = self._wait_for_av1an()
        self._av1an = None
        self._solar_gated.clear()  # only meaningful during video encode - the only pausable phase
        with self._lock:
            self._state.chunk_progress = None
            self._state.solar_paused = False
            self._state.finalizing = False
            self._maybe_close_pause_window_locked()
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
        if self._stop.is_set():
            return

        with self._lock:
            self._state.phase = RunPhase.AUDIO
            self._state.audio_track_count = len(self._config.audio_tracks)
        audio_files = []
        for t, track in enumerate(self._config.audio_tracks):
            with self._lock:
                self._state.audio_track_index = t
            self._log(f"transcoding audio track {t}: {track.title}")
            audio_out = item.out_file.parent / f"{item.out_file.stem}.audio{t}.tmp.mka"
            transcode_audio_track(
                item.src_file,
                track,
                track.source_language or self._config.audio_source_language,
                audio_out,
                speed_correction=self._config.video.speed_correction,
            )
            audio_files.append(audio_out)
        with self._lock:
            self._state.audio_track_index = self._state.audio_track_count
        if self._stop.is_set():
            return

        with self._lock:
            self._state.phase = RunPhase.MUX
        self._log("muxing")
        sub_sources = resolve_subtitle_sources(self._config, item.src_file, episode_tag=None)
        mux_episode(self._config, item.src_file, current_video, audio_files, sub_sources, item.out_file)

        current_video.unlink(missing_ok=True)
        for audio_file in audio_files:
            audio_file.unlink(missing_ok=True)
        self._cleanup_temp_dir(temp_dir)
        if self._stop.is_set():
            return

        with self._lock:
            self._state.phase = RunPhase.INTEGRITY
        result = check_output_integrity(
            item.src_file,
            item.out_file,
            len(self._config.audio_tracks),
            len(self._config.subtitles),
            speed_correction=self._config.video.speed_correction,
        )
        if not result.ok:
            item.out_file.rename(item.out_file.with_suffix(item.out_file.suffix + ".FAILED"))
            raise RuntimeError(f"integrity check failed: {result.reason}")
        self._log(f"integrity OK: {result.reason}")

    def _cleanup_temp_dir(self, temp_dir: Path) -> None:
        """A just-exited av1an/vspipe child can still hold a handle on a chunk file for a moment
        after the process reports done - confirmed live on Windows, where (unlike POSIX) a locked
        file can't be unlinked at all. A previous version used `ignore_errors=True` here, which
        silently ate that failure: the job still reported success while a full chunk folder sat
        orphaned on disk, discovered only much later by manually browsing the output directory.
        One retry after a short pause covers the transient-lock case; a real failure is logged
        instead of swallowed, so it's visible on the dashboard rather than invisible."""
        if not temp_dir.exists():
            return
        try:
            shutil.rmtree(temp_dir)
            return
        except OSError:
            pass
        time.sleep(1.0)
        try:
            shutil.rmtree(temp_dir)
        except OSError as e:
            self._log(f"warning: failed to remove temp dir {temp_dir}: {e}")

    def _wait_for_solar_gate_before_start(self) -> None:
        """Blocks before av1an itself is ever launched, not just suspended after the fact -
        av1an's own startup (VapourSynth indexing, scene detection) is real CPU work that a
        post-start suspend can only interrupt already in progress, not prevent from happening."""
        gate = self._config.solar_gate
        if gate is None or not gate.enabled or self._solar_poller is None:
            return
        with self._lock:
            self._state.waiting_for_solar = True
        self._log(f"waiting for at least {gate.min_watts:.0f}W solar production before starting")
        while not self._stop.is_set() and not self._solar_override.is_set():
            # Av1anRunner.get_progress() only reads done.json from disk - safe to call before
            # the process itself exists, so any progress already resumable from a prior run
            # shows on the dashboard immediately instead of a "0/1" placeholder throughout the
            # entire wait.
            progress = self._av1an.get_progress()
            if progress is not None:
                with self._lock:
                    self._state.frames_done = progress.done_frames
                    self._state.frames_total = progress.total_frames
            if self._solar_poller.is_producing(gate.min_watts) is True:
                break
            time.sleep(_POLL_INTERVAL_SECONDS)
        with self._lock:
            self._state.waiting_for_solar = False
        if self._solar_override.is_set():
            self._log("solar wait skipped by user - starting now")
        elif not self._stop.is_set():
            self._log("solar production confirmed - starting video encode")

    def _wait_for_av1an(self) -> int | None:
        logged_finalizing = False
        while True:
            if self._stop.is_set():
                self._av1an.terminate()
                return None
            self._update_solar_gate()
            progress = self._av1an.get_progress()
            chunk_progress = self._av1an.get_chunk_progress()
            if chunk_progress is not None:
                self._log_newly_finished_chunks(chunk_progress)
            # Every chunk done but av1an's own process hasn't exited yet means it's past
            # per-chunk encoding and into its own final step (mkvmerge concatenation of every
            # chunk into the single output file) - one that reports no progress of its own and,
            # on a long file with many chunks, can take real, non-trivial time. Without this, the
            # dashboard just freezes at "100%" with nothing to explain why nothing looks like
            # it's still happening - confirmed live, looked indistinguishable from a genuine hang.
            all_chunks_done = (
                progress is not None and progress.total_frames > 0
                and progress.done_frames >= progress.total_frames
            )
            no_active_chunks = chunk_progress is not None and not chunk_progress.active
            finalizing = all_chunks_done and no_active_chunks
            if finalizing and not logged_finalizing:
                self._log("all chunks done - concatenating into the final file (mkvmerge)...")
                logged_finalizing = True
            # Solar gating exists to defer real CPU draw, not to stall a nearly-finished item on
            # a disk-bound mkvmerge pass with no meaningful power cost - once finalizing, the gate
            # stops applying (a real user complaint: a whole night's wait to finish a sub-minute
            # merge). Manual pause is left alone here - that's a deliberate user action, not an
            # automatic policy, so it keeps suspending through finalizing same as before.
            self._av1an.set_suspended(
                self._paused.is_set() or (self._solar_gated.is_set() and not finalizing)
            )
            with self._lock:
                if progress is not None:
                    self._state.frames_done = progress.done_frames
                    self._state.frames_total = progress.total_frames
                self._state.chunk_progress = chunk_progress
                self._state.finalizing = finalizing
            exit_code = self._av1an.poll()
            if exit_code is not None:
                return exit_code
            time.sleep(_POLL_INTERVAL_SECONDS)
