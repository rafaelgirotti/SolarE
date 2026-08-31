"""Adapts a real JobRunner's RunState into the JobState shape the dashboard renders."""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path

from solare.engine import JobRunner, RunPhase, TitleConfig
from solare.tui.state import ActiveChunkInfo, JobState


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
        else:
            active_seconds = (
                now - state.item_started_at
            ).total_seconds() - state.item_paused_seconds
            if state.frames_done > 5 and active_seconds > 5 and state.frames_total:
                rate = active_seconds / state.frames_done
                remaining = rate * max(0, state.frames_total - state.frames_done)
                eta_time = now + datetime.timedelta(seconds=remaining)
                pct = 100.0 * state.frames_done / state.frames_total
                eta_text = (
                    f"{eta_time.strftime('%H:%M')} (in {_format_hours_minutes(remaining)}) "
                    f"- {pct:.1f}% done - {state.phase.value}"
                )
            else:
                eta_text = f"calculating... - {state.phase.value}"

        batch_summary = None
        if state.item_count > 1:
            completed = state.item_index - 1
            batch_summary = (
                f"{state.item_index}/{state.item_count} items ({completed} completed) - "
                f"current: {state.current_item_name}"
            )

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
            output_path=str(disk_check_path),
            disk_free_gb=round(disk_usage.free / (1024**3), 1),
            output_used_gb=_sum_output_size_gb(output_root),
            started_at=state.started_at,
            active_chunks=active_chunks,
            waiting_for_solar=state.waiting_for_solar,
            solar_override=state.solar_override,
        )


def _sum_output_size_gb(output_root: Path) -> float:
    if not output_root.exists():
        return 0.0
    total = sum(f.stat().st_size for f in output_root.rglob("*.mkv") if f.is_file())
    return round(total / (1024**3), 2)


def _format_hours_minutes(seconds: float) -> str:
    total_minutes = int(seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m"
