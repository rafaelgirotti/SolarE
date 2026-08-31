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
import asyncio
import datetime
from enum import Enum
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, RichLog, Static

from solare import platform as solare_platform
from solare.engine import (
    JobRunner,
    TitleConfig,
    has_unfinished_work,
    load_config,
    prepend_local_tools_to_path,
)
from solare.hwmonitor import HardwareMonitor, HardwareSnapshot
from solare.solar import GrowattCredentials, SolarPoller
from solare.tui import colors, links
from solare.tui.confirm import ConfirmScreen
from solare.tui.last_job import last_config_path, record_last_config
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
    STOPPING = "stopping"  # stop() requested but the background thread/av1an haven't exited yet


class SolarEApp(App):
    CSS_PATH = Path(__file__).parent / "theme.tcss"
    TITLE = "SolarE"
    BINDINGS = [
        ("c", "choose_config", "Choose config"),
        ("s", "start", "Start"),
        ("p", "toggle_pause", "Pause/Resume"),
        ("t", "stop", "Stop"),
        ("g", "toggle_solar_gate", "Toggle solar gating"),
        ("q", "quit", "Exit"),
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
        self._solar_override_active = False  # mirrors the real JobRunner state - see _render_job_panel
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
            # Same embossed/3D Textual button look as the confirm dialog (see
            # solare/tui/confirm.py) - default (muted, matching No) at rest, variant="success"
            # applied dynamically in _update_controls() to whichever one is the current primary
            # action, same "one green choice, the rest muted" pattern as Yes/No there.
            yield Button("Choose config \\[C]", id="btn_choose")
            yield Button("Start \\[S]", id="btn_start")
            yield Button("Pause \\[P]", id="btn_pause")
            yield Button("Stop \\[T]", id="btn_stop")
            yield Button("Solar Gating: ON \\[G]", id="btn_solar_gate")
            yield Button("Exit \\[Q]", id="btn_exit")
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
        else:
            self._maybe_prompt_resume_last_job()

        self._update_controls()
        self._refresh()
        self.set_interval(_REFRESH_INTERVAL_SECONDS, self._refresh)

    def _maybe_prompt_resume_last_job(self) -> None:
        """Only offered when solare was launched bare (no --config) - an explicit --config on the
        command line already says what to load, so a resume prompt on top would just be a second,
        conflicting suggestion. See solare.tui.last_job for what "last" means."""
        path = last_config_path()
        if path is None or not path.is_file():
            return
        try:
            config = load_config(path)
        except (OSError, KeyError, ValueError):
            return
        if not has_unfinished_work(config):
            return

        def on_answer(resume: bool | None) -> None:
            if resume:
                self._load_config(path)
                self._update_controls()

        self.push_screen(
            ConfirmScreen(f"Resume last job?\n\n[b]{config.title}[/b]\n{config.settings_summary}"),
            on_answer,
        )

    def on_unmount(self) -> None:
        self._hw_monitor.close()
        if self._solar_poller is not None:
            self._solar_poller.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # A modal screen's own button (e.g. ConfirmScreen's Yes/No) is expected to stop() its
        # Button.Pressed event before it bubbles this far - .get() instead of direct indexing is
        # just defense in depth against a future modal that forgets to, so one crashes the whole
        # app instead of silently doing nothing.
        action = {
            "btn_choose": self.action_choose_config,
            "btn_start": self.action_start,
            "btn_pause": self.action_toggle_pause,
            "btn_stop": self.action_stop,
            "btn_solar_gate": self.action_toggle_solar_gate,
            "btn_exit": self.action_quit,
        }.get(event.button.id)
        if action is not None:
            action()

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
        record_last_config(self._config.path)
        self._job_source = LiveJobSource(self._config, runner)
        self._rendered_log_count = 0
        self._solar_override_active = False
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
        # Don't clear job_source / drop to LOADED yet - a real av1an kill isn't instant (worker
        # processes need to actually exit), and doing so here made the dashboard claim "stopped"
        # and offer Start again while av1an was still very much alive in the background. _refresh
        # finalizes the transition once self._job_source.is_alive() genuinely goes False.
        self._phase = AppPhase.STOPPING
        self._update_controls()

    def action_quit(self) -> None:
        """Overrides Textual's own default action_quit (bound to ctrl+q with priority=True by the
        base App class, confirmed via App.BINDINGS - so this is reached even if the user never
        touches the Exit button) - the stock implementation is just self.exit(), immediately,
        with zero regard for a real av1an subprocess that might be mid-encode. That would orphan
        it: the process keeps running with nothing left to pause/resume/suspend it or even show
        its progress, since the dashboard that was tracking it is gone.

        Delegates to a worker rather than doing this inline: push_screen_wait() (needed to await
        the confirm dialog's result before deciding whether to proceed) requires a real Textual
        worker context - confirmed live that even genuine action dispatch (a real keybinding
        press, not just a hand-called coroutine) does NOT provide one on its own, only
        run_worker() does."""
        self.run_worker(self._quit_sequence(), exclusive=True)

    async def _quit_sequence(self) -> None:
        """Confirms first (a plain exit is hard to undo - the whole reason a job needs stopping
        at all), then stops a running job and confirms it has actually exited before quitting,
        same STOPPING mechanism as the Stop button/action_stop - covers a job already stopping
        too (e.g. Stop pressed, then Exit before it finished), not just one just starting to stop
        here."""
        job_active = self._phase in (AppPhase.RUNNING, AppPhase.PAUSED)
        message = "Quit SolarE?" + ("\n\nThis will stop the running job first." if job_active else "")
        confirmed = await self.push_screen_wait(ConfirmScreen(message))
        if not confirmed:
            return
        if job_active:
            self._job_source.stop()
            self._phase = AppPhase.STOPPING
            self._update_controls()
            self.notify("Exiting - stopping the running job first...")
        if self._phase == AppPhase.STOPPING:
            while self._job_source is not None and self._job_source.is_alive():
                await asyncio.sleep(0.5)
        self.exit()

    def action_toggle_solar_gate(self) -> None:
        if self._phase not in (AppPhase.RUNNING, AppPhase.PAUSED) or self._job_source is None:
            return
        self._solar_override_active = not self._solar_override_active
        self._job_source.set_solar_override(self._solar_override_active)
        self._update_controls()

    def _update_controls(self) -> None:
        # Only the current primary/next-step action gets variant="success" (green) - same
        # one-highlighted-choice-the-rest-muted pattern as the confirm dialog's Yes/No, rather
        # than every button rendering green regardless of relevance.
        choose_btn = self.query_one("#btn_choose", Button)
        choose_btn.disabled = self._phase in (
            AppPhase.RUNNING,
            AppPhase.PAUSED,
            AppPhase.STOPPING,
        )
        choose_btn.variant = "success" if self._phase == AppPhase.IDLE else "default"

        start_btn = self.query_one("#btn_start", Button)
        start_btn.disabled = self._phase != AppPhase.LOADED
        start_btn.variant = "success" if self._phase == AppPhase.LOADED else "default"

        pause_btn = self.query_one("#btn_pause", Button)
        pause_btn.disabled = self._phase not in (AppPhase.RUNNING, AppPhase.PAUSED)
        pause_btn.label = "Resume \\[P]" if self._phase == AppPhase.PAUSED else "Pause \\[P]"
        pause_btn.variant = "success" if self._phase in (AppPhase.RUNNING, AppPhase.PAUSED) else "default"

        self.query_one("#btn_stop", Button).disabled = self._phase not in (
            AppPhase.RUNNING,
            AppPhase.PAUSED,
        )
        gate_configured = (
            self._config is not None
            and self._config.solar_gate is not None
            and self._config.solar_gate.enabled
        )
        solar_btn = self.query_one("#btn_solar_gate", Button)
        solar_btn.disabled = not (
            gate_configured and self._phase in (AppPhase.RUNNING, AppPhase.PAUSED)
        )
        solar_btn.label = (
            "Solar Gating: OFF \\[G]" if self._solar_override_active else "Solar Gating: ON \\[G]"
        )

    def _refresh(self) -> None:
        self._render_hw_panel(self._hw_monitor.poll())
        self._render_solar_panel(self._current_solar_state())
        if self._job_source is not None:
            self._render_job_panel(self._job_source.poll_job())
            self._render_log_panel(self._job_source.log_lines)
            if self._phase == AppPhase.STOPPING and not self._job_source.is_alive():
                self._job_source = None
                self._rendered_log_count = 0
                self._phase = AppPhase.LOADED
                self._update_controls()
        else:
            self._render_job_idle()

    def _render_job_idle(self) -> None:
        if self._config is not None:
            text = (
                f"[b]{self._config.title}[/b] loaded - {self._config.settings_summary}\n"
                "Press [b]S[/b] or click [b]Start[/b] below to begin."
            )
            border_title = f" {self._config.title} "
        else:
            text = "No config loaded - press [b]C[/b] or click [b]Choose config[/b] below to pick one."
            border_title = " SolarE "
        self.query_one("#job_meta", Static).update(text)
        self.query_one("#chunks_bar", TextProgressBar).update_progress(0, 1, "")
        self.query_one("#job_footer", Static).update("")
        self.query_one("#job_panel", Vertical).border_title = border_title

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

        pct = 100.0 * job.frames_done / job.frames_total
        bar = self.query_one("#chunks_bar", TextProgressBar)
        bar.update_progress(
            job.frames_done, job.frames_total, f"{job.frames_done}/{job.frames_total} frames - {pct:.1f}%"
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

        # Deliberately just title + phase, no item-count/filename - those already show in the
        # footer (item count) and batch_summary (current filename, batches only), so the always-
        # visible title bar stays short and equally clean for a single-file title or a 24-item
        # batch, instead of growing with a raw scene-release filename or a redundant "(1/1)".
        panel = self.query_one("#job_panel", Vertical)
        panel.border_title = f" {job.title} - {job.phase} "

        if job.solar_override != self._solar_override_active:
            self._solar_override_active = job.solar_override
            self._update_controls()

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
    # Set once, early, before Textual takes over the screen - the title persists in the terminal's
    # own window/tab state regardless of what Textual does with the content area afterward, so
    # this doesn't need to run again on every refresh tick.
    solare_platform.set_console_title("SolarE")
    parser = argparse.ArgumentParser(prog="solare", description="SolarE dashboard")
    parser.add_argument("--config", type=str, default=None, help="Title config .json to load on startup")
    parser.add_argument("--start", action="store_true", help="Start immediately (requires --config)")
    args = parser.parse_args()
    if args.start and not args.config:
        parser.error("--start requires --config")
    SolarEApp(config_path=args.config, auto_start=args.start).run()


if __name__ == "__main__":
    run()
