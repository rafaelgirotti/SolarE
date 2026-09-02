"""Adapts a real JobRunner's RunState into the JobState shape the dashboard renders."""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path

from solare.engine import JobRunner, RunPhase, TitleConfig
from solare.engine.runner import RunState
from solare.tui.state import ActiveChunkInfo, JobState

# (low, high) percent band each phase occupies in the overall cross-phase progress bar - video
# encode dominates real wall-clock time on every title tested so far (audio/mux/integrity are
# each a small tail by comparison), hence the lopsided split. Not derived from measured timing
# per title (that varies with track count etc.) - a fixed, honest-enough approximation so the bar
# stops reading "100%" the moment av1an finishes when 3 more real phases remain.
_PHASE_BANDS = {
    RunPhase.VIDEO_ENCODE: (0.0, 90.0),
    RunPhase.DOLBY_VISION: (90.0, 93.0),
    RunPhase.AUDIO: (93.0, 97.0),
    RunPhase.MUX: (97.0, 99.0),
    RunPhase.INTEGRITY: (99.0, 100.0),
}


def _overall_pct(state: RunState) -> float:
    if state.phase == RunPhase.DONE:
        return 100.0
    low, high = _PHASE_BANDS.get(state.phase, (0.0, 100.0))
    if state.phase == RunPhase.VIDEO_ENCODE and state.frames_total:
        sub_fraction = state.frames_done / state.frames_total
    elif state.phase == RunPhase.AUDIO and state.audio_track_count:
        sub_fraction = state.audio_track_index / state.audio_track_count
    else:
        # DOLBY_VISION/MUX/INTEGRITY have no finer sub-progress to track (each is one subprocess
        # call with no natural midpoint) - landing in the phase's own band at all already says
        # "further than the previous phase," which is the point.
        sub_fraction = 0.0
    return low + (high - low) * sub_fraction


def _batch_eta_text(state: RunState, now: datetime.datetime) -> str | None:
    """Average real active-encode duration of items already completed this run (paused time and
    any pre-start solar wait both excluded - see JobRunner._run_item), times items still ahead -
    a rough per-item pace, not aware of per-title duration variance (a batch mixing long movies
    and short specials would skew this), but still far better than no signal at all for "when
    does the whole batch finish," which nothing previously answered. None before the first item
    finishes (nothing to average yet) or for a single-item run (no "batch" to have an ETA
    distinct from the item's own).

    That average is pure active-encode work, not wall-clock time - solar gating means only part
    of each day is actually spent encoding, so naively adding remaining active-seconds onto `now`
    would understate how long the batch actually takes by however much time nights (and any other
    gaps) add back in. Projected using how much of the batch's own elapsed wall-clock time has
    actually been active so far, rather than separately modeling sunrise/sunset against the solar
    API: whatever fraction of the past was active is the best available estimate for the future
    too, and it self-corrects for weather/season/manual pauses without a second data source."""
    if state.item_count <= 1 or not state.completed_item_seconds:
        return None
    total_active_so_far = sum(state.completed_item_seconds)
    avg_active_seconds = total_active_so_far / len(state.completed_item_seconds)
    items_remaining = state.item_count - state.item_index + 1
    if items_remaining <= 0:
        return None
    remaining_active_seconds = avg_active_seconds * items_remaining

    wall_clock_elapsed = (now - state.started_at).total_seconds()
    if wall_clock_elapsed > 0 and total_active_so_far > 0:
        active_fraction = min(1.0, total_active_so_far / wall_clock_elapsed)
        remaining_seconds = remaining_active_seconds / active_fraction
    else:
        remaining_seconds = remaining_active_seconds

    eta_time = now + datetime.timedelta(seconds=remaining_seconds)
    return _format_eta(remaining_seconds, eta_time, now)


class LiveJobSource:
    def __init__(self, config: TitleConfig, runner: JobRunner) -> None:
        self._config = config
        self._runner = runner

    def pause(self) -> None:
        self._runner.pause()

    def resume(self) -> None:
        self._runner.resume()

    def stop(self) -> None:
        self._runner.stop()

    def is_alive(self) -> bool:
        return self._runner.is_running()

    def set_solar_override(self, active: bool) -> None:
        self._runner.set_solar_override(active)

    @property
    def log_lines(self) -> list[str]:
        return self._runner.get_state().log_lines

    def poll_job(self) -> JobState:
        state = self._runner.get_state()
        now = datetime.datetime.now()

        if state.phase == RunPhase.FAILED:
            eta_text = f"FAILED - {state.error}"
        elif state.phase == RunPhase.DONE:
            eta_text = "done"
        elif self._runner.is_stop_requested():
            eta_text = "stopping - waiting for av1an to exit..."
        elif state.waiting_for_solar:
            eta_text = "waiting for solar production to start"
        elif state.paused:
            eta_text = "paused"
        elif state.solar_paused:
            eta_text = "paused (waiting for sun)"
        elif state.finalizing:
            # Every chunk done but av1an hasn't exited yet - it's running its own mkvmerge
            # concatenation pass, which reports no progress of its own. Without this the normal
            # ETA branch below would show a stale "100% done" with nothing changing, confirmed
            # live to look indistinguishable from a genuine hang on a long file with many chunks.
            eta_text = "all chunks done - finalizing (concatenating into the output file)..."
        else:
            # item_active_seconds only covers *closed* segments - the currently-open one (this
            # branch only runs while actively encoding, so one should be open) isn't in there yet,
            # same reasoning as pause_started_at not being folded into item_paused_seconds until
            # its window closes.
            active_seconds = state.item_active_seconds
            if state.active_segment_started_at is not None:
                active_seconds += (now - state.active_segment_started_at).total_seconds()
            if state.frames_done > 5 and active_seconds > 5 and state.frames_total:
                rate = active_seconds / state.frames_done
                remaining = rate * max(0, state.frames_total - state.frames_done)
                eta_time = now + datetime.timedelta(seconds=remaining)
                # No percentage here - the progress bar already shows overall_pct as its own "X%"
                # right below this line, so a second one here was pure duplication once both
                # showed the same (correct) number. A previous version showed the raw
                # frames_done/frames_total fraction instead, which actively contradicted the
                # bar's number rather than just repeating it - fixed, then found redundant even
                # once fixed.
                eta_text = f"{_format_eta(remaining, eta_time, now)} - {state.phase.value}"
            else:
                eta_text = f"calculating... - {state.phase.value}"

        batch_summary = None
        current_item_name = None
        current_item_src_path = None
        if state.item_count > 1:
            completed = state.item_index - 1
            batch_summary = f"{state.item_index}/{state.item_count} items ({completed} completed)"
            # Split from batch_summary rather than baked into it - the dashboard renders this part
            # as its own non-wrapping, ellipsis-truncating hyperlink to the real source file, which
            # needs the name and path as separate values, not pre-joined into one display string.
            current_item_name = state.current_item_name
            current_item_src_path = state.current_item_src_path
        batch_eta_text = _batch_eta_text(state, now)

        output_root = Path(self._config.output_root)
        disk_check_path = output_root if output_root.exists() else self._config.path.parent
        disk_usage = shutil.disk_usage(disk_check_path)

        # A chunk actively running is frozen bit-for-bit whenever the process tree is suspended,
        # so its wall-clock elapsed time must exclude paused time the same way the ETA calc above
        # does - otherwise it reads as "stuck on this chunk" for however long the pause lasts, when
        # really no time has passed for it at all. Any chunk currently showing as active necessarily
        # started before this pause window opened (av1an can't start a new one while suspended), so
        # subtracting the full accumulated paused-time is exact, not an approximation.
        total_paused_seconds = state.item_paused_seconds
        if state.pause_started_at is not None:
            total_paused_seconds += (now - state.pause_started_at).total_seconds()

        active_chunks = []
        if state.chunk_progress is not None:
            for chunk in state.chunk_progress.active:
                elapsed = max(0.0, chunk.elapsed_seconds() - total_paused_seconds)
                active_chunks.append(
                    ActiveChunkInfo(
                        worker_id=chunk.worker_id,
                        chunk_index=chunk.chunk_index,
                        elapsed_seconds=elapsed,
                        avg_seconds=state.chunk_progress.avg_duration_seconds,
                        stuck=state.chunk_progress.is_stuck(chunk, elapsed_seconds=elapsed),
                    )
                )

        return JobState(
            title=self._config.title,
            phase=state.phase.value,
            item_index=state.item_index,
            item_count=state.item_count,
            frames_done=state.frames_done,
            frames_total=max(state.frames_total, 1),
            settings_summary=self._config.settings_summary,
            config_path=str(self._config.path.resolve()),
            eta_text=eta_text,
            batch_summary=batch_summary,
            batch_eta_text=batch_eta_text,
            current_item_name=current_item_name,
            current_item_src_path=current_item_src_path,
            output_path=str(disk_check_path),
            disk_free_gb=round(disk_usage.free / (1024**3), 1),
            output_used_gb=_sum_output_size_gb(output_root),
            started_at=state.started_at,
            active_chunks=active_chunks,
            waiting_for_solar=state.waiting_for_solar,
            solar_override=state.solar_override,
            overall_pct=_overall_pct(state),
        )


def _sum_output_size_gb(output_root: Path) -> float:
    if not output_root.exists():
        return 0.0
    total = sum(f.stat().st_size for f in output_root.rglob("*.mkv") if f.is_file())
    return round(total / (1024**3), 2)


def _format_duration(seconds: float) -> str:
    """'20d 11h 4m' - never leaves days out and silently overflows hours (a real 491-hour ETA
    read as exactly that, confirmed live, forcing real division to make sense of it). Drops
    leading zero units instead of always showing all three - "4m" for a short wait reads better
    than "0d 0h 4m", and nothing here needs a fixed-width column."""
    total_minutes = int(seconds // 60)
    days, rem_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem_minutes, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _format_eta(remaining_seconds: float, eta_time: datetime.datetime, now: datetime.datetime) -> str:
    """'20d 11h 4m → Tue, Sep 22, 2026 at 19:28' - duration first (how long the wait is),
    then the actual moment it ends. A bare "19:28" used to lead, with the duration parenthesized
    as an afterthought - fine for a same-day ETA, but actively misleading for a multi-day one: a
    bare time-of-day reads as "today at 19:28" with nothing marking it as 20 days out unless you
    separately parse the parenthetical. Year is only shown when the ETA actually lands in a
    different year than now - the one real case being encoding into a queue long enough to cross
    a December 31st, not something worth showing every single time."""
    weekday_and_month_day = eta_time.strftime("%a, %b") + f" {eta_time.day}"
    if eta_time.year != now.year:
        date_part = f"{weekday_and_month_day}, {eta_time.year}"
    else:
        date_part = weekday_and_month_day
    return f"{_format_duration(remaining_seconds)} → {date_part} at {eta_time.strftime('%H:%M')}"
