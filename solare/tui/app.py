"""SolarE dashboard - drives a real encode job end to end.

Hardware stats are real (`solare.hwmonitor`, polled independently of any loaded job - works even
before a config is chosen). Once a config is loaded and started, encode-job progress comes from a
real `solare.engine.JobRunner` (via `LiveJobSource`) - a real `av1an` process, real Dolby
Vision/audio/mux passes, running in a background thread so the dashboard's own refresh tick never
blocks on it. Solar generation is real too (`solare.solar.SolarPoller`, gated on a gitignored
`credentials.json` at the repo root - see README) - only what `plant_energy_data` actually
reports, no weather/PV-string/AC-voltage detail (a different, unwired Growatt endpoint). The same
poller also drives JobRunner's solar-gated auto-pause, when a title config enables it.
"""

from __future__ import annotations

import argparse
import datetime
from enum import Enum
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, RichLog, Static

from solare.engine import JobRunner, TitleConfig, load_config, prepend_local_tools_to_path
from solare.hwmonitor import HardwareMonitor, HardwareSnapshot
from solare.solar import GrowattCredentials, SolarPoller
from solare.tui import colors, links
from solare.tui.live_job import LiveJobSource
from solare.tui.picker import ConfigPickerScreen
from solare.tui.progress_bar import TextProgressBar
from solare.tui.state import ActiveChunkInfo, JobState, SolarState

_REFRESH_INTERVAL_SECONDS = 1.0
_LABEL_WIDTH = 11  # fits "Generation" (10 chars), the longest label used, plus a 1-space gap
_CREDENTIALS_PATH = Path(__file__).resolve().parents[2] / "credentials.json"


def _label(text: str) -> str:
    return f"{text:<{_LABEL_WIDTH}}"


class AppPhase(Enum):
    IDLE = "idle"  # no config loaded
    LOADED = "loaded"  # config loaded, job not started
    RUNNING = "running"
    PAUSED = "paused"


class SolarEApp(App):
    CSS_PATH = Path(__file__).parent / "theme.tcss"
    TITLE = "SolarE"
    BINDINGS = [
        ("c", "choose_config", "Choose config"),
        ("s", "start", "Start"),
        ("p", "toggle_pause", "Pause/Resume"),
        ("t", "stop", "Stop"),
    ]

    def __init__(self, config_path: str | Path | None = None, auto_start: bool = False) -> None:
        super().__init__()
        self._hw_monitor = HardwareMonitor()
        self._job_source: LiveJobSource | None = None
        self._config: TitleConfig | None = None
        self._phase = AppPhase.IDLE
        self._pending_config_path = config_path
        self._pending_auto_start = auto_start
        self._rendered_log_count = 0
        self._solar_poller: SolarPoller | None = None
        self._solar_unavailable_reason: str | None = None
        if _CREDENTIALS_PATH.is_file():
            try:
                self._solar_poller = SolarPoller(GrowattCredentials.from_file(_CREDENTIALS_PATH))
            except RuntimeError as e:
                self._solar_unavailable_reason = str(e)

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
        with Horizontal(id="controls"):
            yield Button("Choose config [C]", id="btn_choose")
            yield Button("Start [S]", id="btn_start")
            yield Button("Pause [P]", id="btn_pause")
            yield Button("Stop [T]", id="btn_stop")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#hw_panel", Static).border_title = " Hardware "
        self.query_one("#solar_panel", Static).border_title = " Solar "
        self.query_one("#log_panel", RichLog).border_title = " Recent log output "

        if self._solar_poller is not None:
            self._solar_poller.start()

        if self._pending_config_path:
            self._load_config(self._pending_config_path)
            if self._pending_auto_start and self._phase == AppPhase.LOADED:
                self.action_start()

        self._update_controls()
        self._refresh()
        self.set_interval(_REFRESH_INTERVAL_SECONDS, self._refresh)

    def on_unmount(self) -> None:
        self._hw_monitor.close()
        if self._solar_poller is not None:
            self._solar_poller.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        {
            "btn_choose": self.action_choose_config,
            "btn_start": self.action_start,
            "btn_pause": self.action_toggle_pause,
            "btn_stop": self.action_stop,
        }[event.button.id]()

    def action_choose_config(self) -> None:
        if self._phase in (AppPhase.RUNNING, AppPhase.PAUSED):
            return
        self.push_screen(ConfigPickerScreen(start_path=Path.cwd()), self._on_config_chosen)

    def _on_config_chosen(self, path: Path | None) -> None:
        if path is not None:
            self._load_config(path)
            self._update_controls()

    def _load_config(self, path: str | Path) -> None:
        try:
            config = load_config(path)
        except (OSError, KeyError, ValueError) as e:
            self.notify(f"Couldn't load {path}: {e}", severity="error")
            return
        self._config = config
        self._job_source = None
        self._rendered_log_count = 0
        self._phase = AppPhase.LOADED

    def action_start(self) -> None:
        if self._phase != AppPhase.LOADED or self._config is None:
            return
        try:
            runner = JobRunner(self._config, solar_poller=self._solar_poller)
        except (OSError, FileNotFoundError, RuntimeError) as e:
            self.notify(f"Couldn't start: {e}", severity="error")
            return
        runner.start()
        self._job_source = LiveJobSource(self._config, runner)
        self._rendered_log_count = 0
        self._phase = AppPhase.RUNNING
        self._update_controls()

    def action_toggle_pause(self) -> None:
        if self._phase == AppPhase.RUNNING:
            self._job_source.pause()
            self._phase = AppPhase.PAUSED
        elif self._phase == AppPhase.PAUSED:
            self._job_source.resume()
            self._phase = AppPhase.RUNNING
        else:
            return
        self._update_controls()

    def action_stop(self) -> None:
        if self._phase not in (AppPhase.RUNNING, AppPhase.PAUSED):
            return
        self._job_source.stop()
        self._job_source = None
        self._rendered_log_count = 0
        self._phase = AppPhase.LOADED
        self._update_controls()

    def _update_controls(self) -> None:
        self.query_one("#btn_choose", Button).disabled = self._phase in (
            AppPhase.RUNNING,
            AppPhase.PAUSED,
        )
        self.query_one("#btn_start", Button).disabled = self._phase != AppPhase.LOADED
        pause_btn = self.query_one("#btn_pause", Button)
        pause_btn.disabled = self._phase not in (AppPhase.RUNNING, AppPhase.PAUSED)
        pause_btn.label = "Resume [P]" if self._phase == AppPhase.PAUSED else "Pause [P]"
        self.query_one("#btn_stop", Button).disabled = self._phase not in (
            AppPhase.RUNNING,
            AppPhase.PAUSED,
        )

    def _refresh(self) -> None:
        self._render_hw_panel(self._hw_monitor.poll())
        self._render_solar_panel(self._current_solar_state())
        if self._job_source is not None:
            self._render_job_panel(self._job_source.poll_job())
            self._render_log_panel(self._job_source.log_lines)
        else:
            self._render_job_idle()

    def _render_job_idle(self) -> None:
        self.query_one("#job_meta", Static).update(
            "No config loaded - press [b]C[/b] or click [b]Choose config[/b] below to pick one."
        )
        self.query_one("#chunks_bar", TextProgressBar).update_progress(0, 1, "")
        self.query_one("#job_footer", Static).update("")
        self.query_one("#job_panel", Vertical).border_title = " SolarE "

    def _render_job_panel(self, job: JobState) -> None:
        elapsed = datetime.datetime.now() - job.started_at
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        meta_lines = [
            f"[b]{_label('Now')}[/b]{now}   [b]Elapsed[/b] {_format_timedelta(elapsed)}"
            f"   [b]ETA[/b] {job.eta_text}",
        ]
        if job.batch_summary:
            meta_lines.append(f"[b]{_label('Batch')}[/b]{job.batch_summary}")
        if job.active_chunks:
            meta_lines.append(f"[b]{_label('Chunks')}[/b]{self._format_active_chunks(job.active_chunks)}")
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

    def _format_active_chunks(self, active_chunks: list[ActiveChunkInfo]) -> str:
        """Per-worker in-progress chunk timing - flags a chunk running far past its recent peers'
        pace as possibly stuck, since a hung worker otherwise looks identical to a slow one until
        someone happens to notice CPU usage has quietly dropped to near-zero."""
        parts = []
        for chunk in active_chunks:
            elapsed = _format_seconds(chunk.elapsed_seconds)
            avg = _format_seconds(chunk.avg_seconds) if chunk.avg_seconds is not None else "n/a"
            text = f"worker {chunk.worker_id}: chunk {chunk.chunk_index} - {elapsed} (avg {avg})"
            if chunk.stuck:
                text = f"[{colors.DANGER}]{text} - POSSIBLY STUCK[/{colors.DANGER}]"
            parts.append(text)
        return "    ".join(parts)

    def _render_hw_panel(self, hw: HardwareSnapshot) -> None:
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
            f"[b]{_label('RAM')}[/b]{hw.ram_used_gb:.1f}/{hw.ram_total_gb:.1f} GB "
            f"([{ram_color}]{hw.ram_load_pct}%[/{ram_color}])"
        )
        self.query_one("#hw_panel", Static).update("\n".join(lines))

    def _current_solar_state(self) -> SolarState | None:
        """None covers every non-happy-path case (no credentials.json, first poll still pending,
        poll failed with nothing cached yet) - the panel renders one explanatory line for all of
        them rather than guessing at placeholder numbers."""
        if self._solar_poller is None:
            return None
        summary, checked_at, error = self._solar_poller.get_latest()
        if summary is None or checked_at is None:
            return None
        age_seconds = (datetime.datetime.now() - checked_at).total_seconds()
        age_text = "now" if age_seconds < 60 else f"{int(age_seconds // 60)}m ago"
        capacity_pct = (
            round(100.0 * summary.current_power_w / summary.nominal_power_w, 1)
            if summary.nominal_power_w
            else 0.0
        )
        line = f"{summary.current_power_w:.0f} W (updated: {age_text})"
        if error:
            line += " [last poll failed, showing stale data]"
        return SolarState(
            line=line,
            ok=summary.current_power_w > 0,
            today_kwh=summary.today_kwh,
            month_kwh=summary.month_kwh,
            total_kwh=summary.total_kwh,
            capacity_pct=capacity_pct,
            nominal_power_w=summary.nominal_power_w,
        )

    def _render_solar_panel(self, solar: SolarState | None) -> None:
        panel = self.query_one("#solar_panel", Static)
        if solar is None:
            if self._solar_unavailable_reason is not None:
                reason = self._solar_unavailable_reason
            elif self._solar_poller is None:
                reason = "not configured - add credentials.json (see README)"
            else:
                reason = "waiting for first Growatt poll..."
            panel.update(f"[{colors.UNKNOWN}]{reason}[/{colors.UNKNOWN}]")
            return

        # Producing-or-not is a status, not a risk gradient - reusing SAFE/WARN/DANGER here would
        # blend into (producing) or clash with (not producing) the rest of the screen's own use of
        # those colors for actual hardware risk. Brightness instead: bold bright phosphor when
        # producing enough, dim/muted when not - closer to how a real monochrome CRT terminal
        # would signal this anyway (one hue, varying intensity), and keeps green/amber/red meaning
        # "risk level" everywhere else on the dashboard.
        color = colors.PHOSPHOR if solar.ok else colors.UNKNOWN
        rated_kw = solar.nominal_power_w / 1000.0
        lines = [
            f"[b]{_label('Generation')}[/b][{color}]{solar.line}[/{color}] "
            f"({solar.capacity_pct}% of {rated_kw:.1f}kW rated capacity)",
            f"[b]{_label('Today')}[/b]{solar.today_kwh} kWh    "
            f"[b]Month[/b] {solar.month_kwh} kWh    "
            f"[b]Total[/b] {solar.total_kwh:,.0f} kWh",
        ]
        self.query_one("#solar_panel", Static).update("\n".join(lines))

    def _render_log_panel(self, log_lines: list[str]) -> None:
        log = self.query_one("#log_panel", RichLog)
        for line in log_lines[self._rendered_log_count :]:
            log.write(line)
        self._rendered_log_count = len(log_lines)


def _format_timedelta(td: datetime.timedelta) -> str:
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _format_seconds(seconds: float) -> str:
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def run() -> None:
    prepend_local_tools_to_path()
    parser = argparse.ArgumentParser(prog="solare", description="SolarE dashboard")
    parser.add_argument("--config", type=str, default=None, help="Title config .json to load on startup")
    parser.add_argument("--start", action="store_true", help="Start immediately (requires --config)")
    args = parser.parse_args()
    if args.start and not args.config:
        parser.error("--start requires --config")
    SolarEApp(config_path=args.config, auto_start=args.start).run()


if __name__ == "__main__":
    run()
