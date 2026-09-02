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
from textual.containers import Vertical
from textual.content import Content
from textual.widgets import Footer, RichLog, Static

from solare import platform as solare_platform
from solare.engine import (
    JobRunner,
    TitleConfig,
    has_unfinished_work,
    load_config,
    prepend_local_tools_to_path,
)
from solare.hwmonitor import HardwareMonitor, HardwareSnapshot
from solare.solar import MAX_READING_AGE_SECONDS, GrowattCredentials, SolarPoller
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


def _safe(value: object) -> Content:
    """Wraps free-form/external text (a filename, a config-authored title, an exception message)
    as literal Content, never parsed as markup - confirmed live that a real subprocess error
    message (a Python list-repr of an ffmpeg command line, itself containing brackets, quotes,
    and `=` signs) crashes Textual's markup parser with a real MarkupError, and that
    `textual.markup.escape()` does NOT reliably prevent this (its regex only escapes `[` followed
    by an actual tag-like character - a `[` followed by a quote, as in a list repr, still trips
    the tokenizer). The only fully robust fix is to never run text like this through the markup
    parser at all. Use this for anything not a hardcoded literal in this file's own source."""
    return Content(str(value))


def _labeled(label: str, value: object) -> Content:
    """A bold hardcoded label (safe as markup) followed by a value that may be free-form/external
    (see _safe) - the label alone goes through Content.from_markup, the value never does."""
    return Content.from_markup(f"[b]{_label(label)}[/b]") + _safe(value)


def _config_summary_content(config: TitleConfig) -> Content:
    """A safe '<bold title>\\n<settings summary>' block for confirm dialogs that need to show
    which config is in play - title/settings_summary are user-authored JSON content, not
    guaranteed free of markup-breaking characters (real titles in this project routinely contain
    brackets, e.g. release-naming conventions like "[BD-Remux]")."""
    return _safe(config.title).stylize("bold") + Content("\n") + _safe(config.settings_summary)


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
            yield Static(id="batch_line")
            yield TextProgressBar(
                id="chunks_bar",
                filled_color=colors.SAFE,
                empty_color="#1a3a1a",
                label_color=colors.PHOSPHOR,
                empty_label_color=colors.UNKNOWN,
            )
            yield Static(id="job_footer")
        yield Static(id="hw_panel")
        yield Static(id="solar_panel")
        # wrap=True (not the default False) - a real log line's full text (a scene-release
        # filename with every audio track spelled out) is routinely wider than the panel, and
        # wrap=False turns that into a horizontal scrollbar whose thumb is nearly always near
        # full-width - looks chunky no matter how thin scrollbar-size-horizontal is set (a
        # terminal cell is taller than it is wide, so a 1-row scrollbar reads thicker than a
        # 1-column one regardless), and needs manual horizontal scrolling to read a cut-off line
        # at all. Wrapping trades fewer visible log entries per screen for no scrollbar and every
        # line fully readable without scrolling - the better trade for a log meant to be scanned.
        yield RichLog(id="log_panel", max_lines=500, wrap=True, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#hw_panel", Static).border_title = " Hardware "
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
            if not resume:
                return
            if self._load_config(path):
                self._update_controls()
                self.action_start()

        self.push_screen(
            ConfirmScreen(Content("Resume last job?\n\n") + _config_summary_content(config)),
            on_answer,
        )

    def on_unmount(self) -> None:
        self._hw_monitor.close()
        if self._solar_poller is not None:
            self._solar_poller.stop()

    def action_choose_config(self) -> None:
        if self._phase in (AppPhase.RUNNING, AppPhase.PAUSED):
            return
        self.push_screen(ConfigPickerScreen(start_path=Path.cwd()), self._on_config_chosen)

    def _on_config_chosen(self, path: Path | None) -> None:
        if path is None or not self._load_config(path):
            return
        self._update_controls()

        def on_answer(start: bool | None) -> None:
            if start:
                self.action_start()

        self.push_screen(
            ConfirmScreen(
                Content("Config loaded:\n\n")
                + _config_summary_content(self._config)
                + Content("\n\nStart now?")
            ),
            on_answer,
        )

    def _load_config(self, path: str | Path) -> bool:
        try:
            config = load_config(path)
        except (OSError, KeyError, ValueError) as e:
            self.notify(f"Couldn't load {path}: {e}", severity="error")
            return False
        self._config = config
        self._job_source = None
        self._rendered_log_count = 0
        self._phase = AppPhase.LOADED
        return True

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
        # The Footer is the only control surface now (see check_action for what's actually
        # enabled/disabled per action) - this just prompts it to re-poll check_action after a
        # state change, same call sites that used to also touch a big Button row directly.
        self.refresh_bindings()

    def _gate_configured(self) -> bool:
        return (
            self._config is not None
            and self._config.solar_gate is not None
            and self._config.solar_gate.enabled
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Grays out (but keeps visible) a Footer key binding whose action doesn't apply to the
        current AppPhase - same rules the old per-Button .disabled logic used, now expressed once
        here instead of duplicated across six Button widgets. None means "disabled but still
        shown, not clickable" (Textual's own semantics - False would hide the entry entirely,
        which loses the at-a-glance "this exists, just not right now" a real disabled control
        gives)."""
        if action == "choose_config":
            return None if self._phase in (AppPhase.RUNNING, AppPhase.PAUSED, AppPhase.STOPPING) else True
        if action == "start":
            return True if self._phase == AppPhase.LOADED else None
        if action in ("toggle_pause", "stop"):
            return True if self._phase in (AppPhase.RUNNING, AppPhase.PAUSED) else None
        if action == "toggle_solar_gate":
            return (
                True
                if self._gate_configured() and self._phase in (AppPhase.RUNNING, AppPhase.PAUSED)
                else None
            )
        return True

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
                _safe(self._config.title).stylize("bold")
                + Content.from_markup(" loaded - ")
                + _safe(self._config.settings_summary)
                + Content.from_markup("\nPress [b]S[/b] or click [b]Start[/b] below to begin.")
            )
            border_title = Content(" ") + _safe(self._config.title) + Content(" ")
        else:
            text = Content.from_markup(
                "No config loaded - press [b]C[/b] or click [b]Choose config[/b] below to pick one."
            )
            border_title = Content(" SolarE ")
        self.query_one("#job_meta", Static).update(text)
        self.query_one("#batch_line", Static).display = False  # otherwise a batch's current-item
        # line from a previous run stays visible, stale, once the job stops and this idle view
        # takes over - _render_job_panel is the only other place that touches this widget.
        self.query_one("#chunks_bar", TextProgressBar).update_progress(0, 1, "")
        self.query_one("#job_footer", Static).update("")
        self.query_one("#job_panel", Vertical).border_title = border_title

    def _render_job_panel(self, job: JobState) -> None:
        # job.eta_text/batch_summary/title and any hyperlink display text are all free-form -
        # eta_text in particular can be a raw subprocess exception message (a Python list-repr of
        # a full ffmpeg command line) once a job fails, confirmed live to crash Textual's markup
        # parser with a real MarkupError if concatenated into a plain f-string ever passed to
        # Content.from_markup()/.update(). Built via Content concatenation instead (see _safe/
        # _labeled) so free-form text is never parsed as markup, no matter what it contains.
        elapsed = datetime.datetime.now() - job.started_at
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

        meta = (
            _labeled("Now", now)
            + Content.from_markup(f"   [b]Elapsed[/b] {_format_timedelta(elapsed)}   [b]ETA[/b] ")
            + _safe(job.eta_text)
        )
        if job.active_chunks:
            meta = (
                meta
                + Content("\n")
                + Content.from_markup(f"[b]{_label('Chunks')}[/b]")
                + self._format_active_chunks(job.active_chunks)
            )
        self.query_one("#job_meta", Static).update(meta)

        # Its own widget, not folded into job_meta above - text-wrap/text-overflow are per-widget
        # CSS properties (see theme.tcss), and the current item's name is the one piece here that
        # should truncate with an ellipsis at the line edge instead of wrapping, while everything
        # else in the panel keeps wrapping normally. The hyperlink goes last so it's what actually
        # gets cut when the line is too long, not the item-count/ETA text ahead of it.
        batch_line = self.query_one("#batch_line", Static)
        batch_line.display = job.batch_summary is not None
        if job.batch_summary is not None:
            line = _labeled("Batch", job.batch_summary)
            if job.batch_eta_text:
                line = line + Content("   ") + _labeled("Batch ETA", job.batch_eta_text)
            if job.current_item_name and job.current_item_src_path:
                line = (
                    line
                    + Content("   current: ")
                    + links.hyperlink(job.current_item_name, job.current_item_src_path)
                )
            batch_line.update(line)

        # overall_pct spans every phase (video encode + audio + mux + integrity), not just
        # frames_done/frames_total - a frame-only percentage hit 100% the moment av1an finished
        # while 3 more real phases (each taking real time) were still ahead, confirmed live to
        # read as "done" well before the job actually was. Frame counts stay in the label during
        # video encoding specifically, since that detail is still meaningful there.
        bar_label = (
            f"{job.frames_done}/{job.frames_total} frames - {job.overall_pct:.1f}%"
            if job.phase == "video encoding"
            else f"{job.overall_pct:.1f}% - {job.phase}"
        )
        bar = self.query_one("#chunks_bar", TextProgressBar)
        bar.update_progress(job.overall_pct, 100.0, bar_label)

        disk_color = colors.falling_gradient(
            job.disk_free_gb, colors.DISK_FREE_WARN_GB, colors.DISK_FREE_DANGER_GB
        )
        completed_items = max(0, job.item_index - 1)
        config_link = links.hyperlink(Path(job.config_path).name, job.config_path)
        disk_path_link = links.hyperlink(links.shorten_path(job.output_path), job.output_path)
        footer = (
            _labeled("Settings", job.settings_summary)
            + Content.from_markup(" - config: ")
            + config_link
            + Content.from_markup(
                f"\n[b]{_label('Disk')}[/b][{disk_color}]{job.disk_free_gb}GB free[/{disk_color}] on "
            )
            + disk_path_link
            + Content.from_markup(
                f"    [b]Output used[/b] {job.output_used_gb}GB "
                f"({completed_items}/{job.item_count} items)"
            )
        )
        self.query_one("#job_footer", Static).update(footer)

        # Deliberately just title + phase, no item-count/filename - those already show in the
        # footer (item count) and the Batch line (current filename, batches only), so the always-
        # visible title bar stays short and equally clean for a single-file title or a 24-item
        # batch, instead of growing with a raw scene-release filename or a redundant "(1/1)".
        panel = self.query_one("#job_panel", Vertical)
        panel.border_title = Content(" ") + _safe(job.title) + Content.from_markup(f" - {job.phase} ")

        if job.solar_override != self._solar_override_active:
            self._solar_override_active = job.solar_override
            self._update_controls()

    def _format_active_chunks(self, active_chunks: list[ActiveChunkInfo]) -> Content:
        """Per-worker in-progress chunk timing - flags a chunk running far past its recent peers'
        pace as possibly stuck, since a hung worker otherwise looks identical to a slow one until
        someone happens to notice CPU usage has quietly dropped to near-zero. Every field used
        here is numeric or digit-constrained (chunk_index is regex-captured as \\d+ in
        chunk_progress.py) - safe to build via Content.from_markup directly, unlike the rest of
        this panel's free-form text."""
        parts = []
        for chunk in active_chunks:
            elapsed = _format_seconds(chunk.elapsed_seconds)
            avg = _format_seconds(chunk.avg_seconds) if chunk.avg_seconds is not None else "n/a"
            text = f"worker {chunk.worker_id}: chunk {chunk.chunk_index} - {elapsed} (avg {avg})"
            if chunk.stuck:
                text = f"[{colors.DANGER}]{text} - POSSIBLY STUCK[/{colors.DANGER}]"
            parts.append(text)
        return Content.from_markup("    ".join(parts))

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
        gpu_fields = (hw.gpu_temp_c, hw.gpu_load_pct, hw.gpu_power_w, hw.gpu_mem_used_mb, hw.gpu_mem_total_mb)
        if any(v is not None for v in gpu_fields):
            # Each NVML call is independent and can fail on its own (confirmed live: a GPU driver
            # update mid-poll made nvmlDeviceGetMemoryInfo throw while temp/load/power - read
            # earlier in the same call - had already succeeded) - monitor.py's single try/except
            # around all of them means any subset can come back None while the rest are real
            # values, so each field here needs its own "n/a" fallback rather than assuming the
            # whole GPU is either fully present or fully absent.
            gpu_temp_color = colors.rising_gradient(
                hw.gpu_temp_c, colors.GPU_TEMP_WARN_C, colors.GPU_TEMP_DANGER_C
            )
            gpu_load = f"{hw.gpu_load_pct}% load" if hw.gpu_load_pct is not None else "load n/a"
            gpu_temp = (
                f"[{gpu_temp_color}]{hw.gpu_temp_c}C[/{gpu_temp_color}]"
                if hw.gpu_temp_c is not None
                else "n/a"
            )
            gpu_power = f"{hw.gpu_power_w}W" if hw.gpu_power_w is not None else "n/a"
            if hw.gpu_mem_used_mb is not None and hw.gpu_mem_total_mb is not None:
                gpu_mem = f"{hw.gpu_mem_used_mb / 1024:.1f}/{hw.gpu_mem_total_mb / 1024:.1f} GB"
            else:
                gpu_mem = "n/a"
            lines.append(
                f"[b]{_label('GPU')}[/b]{gpu_load}, temp {gpu_temp}, {gpu_power}, {gpu_mem}"
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
        return SolarState(
            line=f"{summary.current_power_w:.0f} W (updated: {age_text})",
            ok=summary.current_power_w > 0,
            today_kwh=summary.today_kwh,
            month_kwh=summary.month_kwh,
            total_kwh=summary.total_kwh,
            capacity_pct=capacity_pct,
            nominal_power_w=summary.nominal_power_w,
            # Not just bool(error) - a reading that's aged past MAX_READING_AGE_SECONDS is exactly
            # as untrustworthy as one from a poll that just failed, same rule is_producing() uses
            # for the gating decision. Otherwise a real multi-hour outage would keep showing a
            # confident, unflagged number as long as the *next* poll attempt hadn't run yet.
            stale=bool(error) or age_seconds > MAX_READING_AGE_SECONDS,
        )

    def _render_solar_panel(self, solar: SolarState | None) -> None:
        panel = self.query_one("#solar_panel", Static)
        # Gating on/off belongs with the rest of the solar-related status, not the job panel -
        # moved here (into the panel's own title, always visible, never competing for a line with
        # Now/Elapsed/ETA/Batch) after living briefly as a job_meta line that worked but wasn't
        # the right home for it.
        if self._gate_configured():
            gate_color = colors.WARN if self._solar_override_active else colors.SAFE
            gate_text = "OFF" if self._solar_override_active else "ON"
            panel.border_title = (
                Content(" Solar - Gating: ")
                + Content(gate_text).stylize(gate_color)
                + Content(" ")
            )
        else:
            panel.border_title = Content(" Solar ")
        if solar is None:
            if self._solar_unavailable_reason is not None:
                # A real exception message (Growatt API/credential-loading failure) - free-form,
                # same category as job.eta_text above, so it goes through _safe() too.
                reason = _safe(self._solar_unavailable_reason)
            elif self._solar_poller is None:
                reason = Content("not configured - add credentials.json (see README)")
            else:
                reason = Content("waiting for first Growatt poll...")
            panel.update(reason.stylize(colors.UNKNOWN))
            return

        # Producing-or-not is a status, not a risk gradient - reusing SAFE/WARN/DANGER here would
        # blend into (producing) or clash with (not producing) the rest of the screen's own use of
        # those colors for actual hardware risk. Brightness instead: bold bright phosphor when
        # producing enough, dim/muted when not - closer to how a real monochrome CRT terminal
        # would signal this anyway (one hue, varying intensity), and keeps green/amber/red meaning
        # "risk level" everywhere else on the dashboard.
        color = colors.PHOSPHOR if solar.ok else colors.UNKNOWN
        rated_kw = solar.nominal_power_w / 1000.0
        content = Content.from_markup(
            f"[b]{_label('Generation')}[/b][{color}]{solar.line}[/{color}] "
            f"({solar.capacity_pct}% of {rated_kw:.1f}kW rated capacity)"
        )
        if solar.stale:
            # A hardcoded literal, but still not safe to splice into an f-string bound for
            # Content.from_markup() - it starts with "[l", which the tokenizer treats as a
            # tag-open attempt the same way a real bracket-heavy value would. _safe() sidesteps
            # that the same way it does for genuinely free-form text; .stylize() applies the
            # warn color directly instead of needing a markup open/close pair (which can't span
            # across concatenated Content objects - each from_markup() call must be self-balanced).
            content = content + Content(" ") + _safe("[last poll failed, showing stale data]").stylize(
                colors.WARN
            )
        content = content + Content.from_markup(
            f"\n[b]{_label('Today')}[/b]{solar.today_kwh} kWh    "
            f"[b]Month[/b] {solar.month_kwh} kWh    "
            f"[b]Total[/b] {solar.total_kwh:,.0f} kWh"
        )
        self.query_one("#solar_panel", Static).update(content)

    def _render_log_panel(self, log_lines: list[str]) -> None:
        log = self.query_one("#log_panel", RichLog)
        for line in log_lines[self._rendered_log_count :]:
            log.write(line)
        self._rendered_log_count = len(log_lines)


def _format_timedelta(td: datetime.timedelta) -> str:
    """'1m 17s' for a short elapsed, dropping to 'Xh Ym'/'Xd Yh Zm' (no seconds) once there's a
    bigger unit worth leading with - matches the same drop-leading-zero-units style as
    live_job._format_duration, just with a seconds tier since elapsed (unlike an ETA countdown)
    is watched live and second-level feedback actually matters at the short end."""
    total_seconds = int(td.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {seconds}s"


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
