"""SolarE dashboard shell - Phase 3: real layout and retro styling, mock encode-job data.

Hardware stats are real (`solare.hwmonitor`, read live from this machine). Encode-job progress
(chunks, ETA, log lines) and solar generation are simulated via `mock.py` - there's no
`solare.engine` orchestration yet, see docs/ARCHITECTURE.md and the README roadmap.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, RichLog, Static

from solare.tui import colors, links
from solare.tui.mock import MockDataSource
from solare.tui.progress_bar import TextProgressBar
from solare.tui.state import DashboardState

_REFRESH_INTERVAL_SECONDS = 1.0
_LABEL_WIDTH = 11  # fits "Generation" (10 chars), the longest label used, plus a 1-space gap


def _label(text: str) -> str:
    return f"{text:<{_LABEL_WIDTH}}"


class SolarEApp(App):
    CSS_PATH = Path(__file__).parent / "theme.tcss"
    TITLE = "SolarE"

    def __init__(self) -> None:
        super().__init__()
        self._data_source = MockDataSource(item_index=3, item_count=12)
        self._rendered_log_count = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="job_panel"):
            yield Static(id="job_meta")
            yield TextProgressBar(
                id="chunks_bar",
                filled_color=colors.SAFE,
                empty_color="#1a3a1a",
                label_color=colors.PHOSPHOR,
            )
            yield Static(id="job_footer")
        yield Static(id="hw_panel")
        yield Static(id="solar_panel")
        yield RichLog(id="log_panel", max_lines=500, wrap=False, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#hw_panel", Static).border_title = " Hardware "
        self.query_one("#solar_panel", Static).border_title = " Solar "
        self.query_one("#log_panel", RichLog).border_title = " Recent log output "
        self._refresh()
        self.set_interval(_REFRESH_INTERVAL_SECONDS, self._refresh)

    def on_unmount(self) -> None:
        self._data_source.close()

    def _refresh(self) -> None:
        state = self._data_source.poll()
        self._render_job_panel(state)
        self._render_hw_panel(state)
        self._render_solar_panel(state)
        self._render_log_panel(state)

    def _render_job_panel(self, state: DashboardState) -> None:
        job = state.job
        elapsed = datetime.datetime.now() - job.started_at
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        meta_lines = [
            f"[b]{_label('Now')}[/b]{now}   [b]Elapsed[/b] {_format_timedelta(elapsed)}"
            f"   [b]ETA[/b] {job.eta_text}",
        ]
        if job.batch_summary:
            meta_lines.append(f"[b]{_label('Batch')}[/b]{job.batch_summary}")
        self.query_one("#job_meta", Static).update("\n".join(meta_lines))

        pct = 100.0 * job.chunks_done / job.chunks_total
        bar = self.query_one("#chunks_bar", TextProgressBar)
        bar.update_progress(
            job.chunks_done, job.chunks_total, f"{job.chunks_done}/{job.chunks_total} chunks - {pct:.1f}%"
        )

        disk_color = colors.falling_gradient(
            job.disk_free_gb, colors.DISK_FREE_WARN_GB, colors.DISK_FREE_DANGER_GB
        )
        completed_items = max(0, job.item_index - 1)
        config_link = links.hyperlink(Path(job.config_path).name, job.config_path)
        disk_path_link = links.hyperlink(links.shorten_path(job.output_path), job.output_path)
        footer_lines = [
            f"[b]{_label('Settings')}[/b]{job.settings_summary} - config: {config_link}",
            f"[b]{_label('Disk')}[/b][{disk_color}]{job.disk_free_gb}GB free[/{disk_color}] on "
            f"{disk_path_link}    [b]Output used[/b] {job.output_used_gb}GB "
            f"({completed_items}/{job.item_count} items)",
        ]
        self.query_one("#job_footer", Static).update("\n".join(footer_lines))

        panel = self.query_one("#job_panel", Vertical)
        panel.border_title = f" {job.title} - {job.phase} ({job.item_index}/{job.item_count}) "

    def _render_hw_panel(self, state: DashboardState) -> None:
        hw = state.hw
        temp_color = colors.rising_gradient(
            hw.cpu_temp_c, colors.CPU_TEMP_WARN_C, colors.CPU_TEMP_DANGER_C
        )
        temp = f"[{temp_color}]{hw.cpu_temp_c}C[/{temp_color}]" if hw.cpu_temp_c is not None else "n/a"
        power = f"{hw.cpu_power_w}W" if hw.cpu_power_w is not None else "n/a"
        lines = [
            f"[b]{_label('CPU')}[/b]{hw.cpu_total_load_pct}% total, {hw.cpu_max_core_load_pct}% "
            f"max core, temp {temp}, power {power}",
        ]
        if hw.gpu_load_pct is not None:
            gpu_temp_color = colors.rising_gradient(
                hw.gpu_temp_c, colors.GPU_TEMP_WARN_C, colors.GPU_TEMP_DANGER_C
            )
            gpu_mem_used_gb = hw.gpu_mem_used_mb / 1024
            gpu_mem_total_gb = hw.gpu_mem_total_mb / 1024
            lines.append(
                f"[b]{_label('GPU')}[/b]{hw.gpu_load_pct}% load, "
                f"[{gpu_temp_color}]{hw.gpu_temp_c}C[/{gpu_temp_color}], "
                f"{hw.gpu_power_w}W, {gpu_mem_used_gb:.1f}/{gpu_mem_total_gb:.1f} GB"
            )
        else:
            lines.append(f"[b]{_label('GPU')}[/b]n/a (no NVIDIA GPU detected, or the gpu extra isn't installed)")
        ram_color = colors.rising_gradient(
            hw.ram_load_pct, colors.RAM_LOAD_WARN_PCT, colors.RAM_LOAD_DANGER_PCT
        )
        lines.append(
            f"[b]{_label('RAM')}[/b]{hw.ram_used_gb} GB used ([{ram_color}]{hw.ram_load_pct}%[/{ram_color}])"
        )
        self.query_one("#hw_panel", Static).update("\n".join(lines))

    def _render_solar_panel(self, state: DashboardState) -> None:
        solar = state.solar
        # Producing-or-not is a status, not a risk gradient - reusing SAFE/WARN/DANGER here would
        # blend into (producing) or clash with (not producing) the rest of the screen's own use of
        # those colors for actual hardware risk. Brightness instead: bold bright phosphor when
        # producing enough, dim/muted when not - closer to how a real monochrome CRT terminal
        # would signal this anyway (one hue, varying intensity), and keeps green/amber/red meaning
        # "risk level" everywhere else on the dashboard.
        color = colors.PHOSPHOR if solar.ok else colors.UNKNOWN
        pv_text = "  ".join(f"PV{i + 1} {v}V/{a}A" for i, (v, a) in enumerate(solar.pv_strings))

        weather_color = colors.weather_color(solar.weather_temp_c)
        weather_temp = f"{solar.weather_temp_c}C"
        if weather_color:
            weather_temp = f"[{weather_color}]{weather_temp}[/{weather_color}]"

        lines = [
            f"[b]{_label('Generation')}[/b][{color}]{solar.line}[/{color}] "
            f"({solar.capacity_pct}% of rated capacity)",
            f"[b]{_label('Today')}[/b]{solar.today_kwh} kWh    "
            f"[b]Month[/b] {solar.month_kwh} kWh    "
            f"[b]Total[/b] {solar.total_kwh:,.0f} kWh",
            f"[b]{_label('Weather')}[/b]{solar.weather_condition}, {weather_temp}",
            f"[b]{_label('Inverter')}[/b]{pv_text}    AC {solar.ac_voltage}V/{solar.ac_frequency}Hz",
        ]
        self.query_one("#solar_panel", Static).update("\n".join(lines))

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
