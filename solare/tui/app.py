"""SolarE dashboard shell - Phase 3: real layout and retro styling, mock encode-job data.

Hardware stats are real (`solare.hwmonitor`, read live from this machine). Encode-job progress
(chunks, ETA, log lines) is simulated via `mock.py` - there's no `solare.engine` orchestration
yet, see docs/ARCHITECTURE.md and the README roadmap.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, RichLog, Static

from solare.tui.mock import MockDataSource
from solare.tui.state import DashboardState

_REFRESH_INTERVAL_SECONDS = 1.0


class SolarEApp(App):
    CSS_PATH = Path(__file__).parent / "theme.tcss"
    TITLE = "SolarE"

    def __init__(self) -> None:
        super().__init__()
        self._data_source = MockDataSource()
        self._rendered_log_count = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="dashboard"):
            yield Static(id="job_panel")
            yield Static(id="hw_panel")
            yield RichLog(id="log_panel", max_lines=500, wrap=False, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#hw_panel", Static).border_title = " Hardware "
        self.query_one("#log_panel", RichLog).border_title = " Recent log output "
        self._refresh()
        self.set_interval(_REFRESH_INTERVAL_SECONDS, self._refresh)

    def on_unmount(self) -> None:
        self._data_source.close()

    def _refresh(self) -> None:
        state = self._data_source.poll()
        self._render_job_panel(state)
        self._render_hw_panel(state)
        self._render_log_panel(state)

    def _render_job_panel(self, state: DashboardState) -> None:
        job = state.job
        elapsed = datetime.datetime.now() - job.started_at
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pct = 100.0 * job.chunks_done / job.chunks_total
        solar_color = "green" if job.solar_ok else "yellow"
        lines = [
            f"[b]Now[/b]      {now}      [b]Elapsed[/b]  {_format_timedelta(elapsed)}",
            f"[b]Job[/b]      {job.job_name} | Chunks: {job.chunks_done}/{job.chunks_total} ({pct:.1f}%)",
            f"[b]Settings[/b] {job.settings_summary}",
            f"[b]ETA[/b]      {job.eta_item}",
            f"[b]Disk[/b]     {job.disk_free_gb}GB free on {job.disk_drive}",
            f"[b]Solar[/b]    [{solar_color}]{job.solar_line}[/{solar_color}]",
        ]
        panel = self.query_one("#job_panel", Static)
        panel.update("\n".join(lines))
        panel.border_title = f" {job.title} - {job.phase} ({job.item_index}/{job.item_count}) "

    def _render_hw_panel(self, state: DashboardState) -> None:
        hw = state.hw
        temp = f"{hw.cpu_temp_c}C" if hw.cpu_temp_c is not None else "n/a"
        power = f"{hw.cpu_power_w}W" if hw.cpu_power_w is not None else "n/a"
        lines = [
            f"CPU  {hw.cpu_total_load_pct}% total, {hw.cpu_max_core_load_pct}% max core, "
            f"temp {temp}, power {power}",
        ]
        if hw.gpu_load_pct is not None:
            lines.append(
                f"GPU  {hw.gpu_load_pct}% load, {hw.gpu_temp_c}C, {hw.gpu_power_w}W, "
                f"{hw.gpu_mem_used_mb}/{hw.gpu_mem_total_mb} MB"
            )
        else:
            lines.append("GPU  n/a (no NVIDIA GPU detected, or the gpu extra isn't installed)")
        lines.append(f"RAM  {hw.ram_used_gb} GB used ({hw.ram_load_pct}%)")
        self.query_one("#hw_panel", Static).update("\n".join(lines))

    def _render_log_panel(self, state: DashboardState) -> None:
        log = self.query_one("#log_panel", RichLog)
        for line in state.log_lines[self._rendered_log_count :]:
            log.write(line)
        self._rendered_log_count = len(state.log_lines)


def _format_timedelta(td: datetime.timedelta) -> str:
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def run() -> None:
    SolarEApp().run()


if __name__ == "__main__":
    run()
