"""Adapts a real JobRunner's RunState into the JobState shape the dashboard renders - the same
interface `mock.MockJobSource` exposes (`poll_job()`, `log_lines`, `pause`/`resume`), so `app.py`'s
rendering code doesn't change depending on which one is behind it.
"""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path

from solare.engine import JobRunner, RunPhase, TitleConfig
from solare.tui.state import JobState


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
        elif state.paused:
            eta_text = "paused"
        else:
            active_seconds = (
                now - state.item_started_at
            ).total_seconds() - state.item_paused_seconds
            if state.chunks_done > 5 and active_seconds > 5 and state.chunks_total:
                rate = active_seconds / state.chunks_done
                remaining = rate * max(0, state.chunks_total - state.chunks_done)
                eta_time = now + datetime.timedelta(seconds=remaining)
                pct = 100.0 * state.chunks_done / state.chunks_total
                eta_text = (
                    f"{eta_time.strftime('%H:%M')} (in {_format_hours_minutes(remaining)}) "
                    f"- {pct:.1f}% done - {state.phase.value}"
                )
            else:
                eta_text = f"calculating... - {state.phase.value}"

        batch_summary = None
        if state.item_count > 1:
            completed = state.item_index - 1
            batch_summary = f"{state.item_index}/{state.item_count} items ({completed} completed)"

        output_root = Path(self._config.output_root)
        disk_check_path = output_root if output_root.exists() else self._config.path.parent
        disk_usage = shutil.disk_usage(disk_check_path)

        return JobState(
            title=self._config.title,
            phase=state.current_item_name or state.phase.value,
            item_index=state.item_index,
            item_count=state.item_count,
            chunks_done=state.chunks_done,
            chunks_total=max(state.chunks_total, 1),
            settings_summary=self._config.settings_summary,
            config_path=str(self._config.path.resolve()),
            eta_text=eta_text,
            batch_summary=batch_summary,
            output_path=str(disk_check_path),
            disk_free_gb=round(disk_usage.free / (1024**3), 1),
            output_used_gb=_sum_output_size_gb(output_root),
            started_at=state.started_at,
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
