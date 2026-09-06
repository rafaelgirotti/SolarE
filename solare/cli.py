"""Fully headless job runner - no Textual, no terminal rendering, plain append-only log lines
safe for stdout redirected to a file, a pipe, or no real TTY at all (confirmed live: launched via
a plain subprocess with stdout captured, not attached to a terminal, exactly the case
`solare.tui.app.SolarEApp.run()` can't handle - Textual's App.run() requires a real terminal).

This drives the exact same `solare.engine.JobRunner` the dashboard does - JobRunner already has
zero Textual dependency (see runner.py's own docstring/imports), so nothing about "real work" is
duplicated here; this is a second, minimal *view* over it, the same relationship
`solare.tui.app.SolarEApp` already has. Anything wired into JobRunner (solar gating, pause/resume,
Dolby Vision/audio/mux/integrity) works identically here, just reported as scrolling text instead
of a live-redrawing panel.
"""

from __future__ import annotations

import signal
import sys
import time
import types
from pathlib import Path

from solare.engine import JobRunner, RunPhase, load_config, prepend_local_tools_to_path
from solare.solar import GrowattCredentials, SolarPoller

_POLL_INTERVAL_SECONDS = 1.0
_CREDENTIALS_PATH = Path(__file__).resolve().parent.parent / "credentials.json"


def run_headless(config_path: str, skip_solar_gate: bool = False) -> int:
    """Returns a real process exit code (0 success, 1 failure, 130 on Ctrl+C - the conventional
    128+SIGINT - matching normal CLI conventions so a wrapping script can check $?/errorlevel
    without parsing log text)."""
    prepend_local_tools_to_path()
    config = load_config(config_path)
    print(f"loaded: {config.title}\n{config.settings_summary}", flush=True)

    solar_poller: SolarPoller | None = None
    if _CREDENTIALS_PATH.is_file():
        try:
            solar_poller = SolarPoller(GrowattCredentials.from_file(_CREDENTIALS_PATH))
            solar_poller.start()
        except RuntimeError as e:
            print(f"solar gating unavailable: {e}", file=sys.stderr, flush=True)

    try:
        runner = JobRunner(config, solar_poller=solar_poller)
    except (OSError, FileNotFoundError, RuntimeError) as e:
        # Matches solare.tui.app.SolarEApp.action_start()'s own exception handling - JobRunner's
        # constructor builds the real queue synchronously (see build_queue()), so a bad source
        # path/pattern fails here, before start() and before any subprocess ever launches.
        print(f"couldn't start: {e}", file=sys.stderr, flush=True)
        if solar_poller is not None:
            solar_poller.stop()
        return 1
    if skip_solar_gate:
        # Set before start() - _solar_override is a plain threading.Event, safe to set before the
        # background thread even exists, which avoids any race against
        # _wait_for_solar_gate_before_start()'s own check (it re-polls every second regardless, so
        # setting this after start() would only cost up to ~1s, but there's no reason to accept
        # even that when setting it first is just as easy and race-free by construction).
        runner.set_solar_override(True)
        print("solar gating skipped (--skip-solar-gate)", flush=True)
    runner.start()

    logged_count = 0
    last_phase: RunPhase | None = None
    stop_requested = False

    def handle_sigint(signum: int, frame: types.FrameType | None) -> None:
        nonlocal stop_requested
        if stop_requested:
            return  # second Ctrl+C - let Python's default handler take over and kill it outright
        stop_requested = True
        print(
            "\nstopping (Ctrl+C) - waiting for the current subprocess to exit cleanly, "
            "not killing it mid-write...",
            flush=True,
        )
        runner.stop()

    previous_handler = signal.signal(signal.SIGINT, handle_sigint)
    try:
        while runner.is_running():
            state = runner.get_state()
            for line in state.log_lines[logged_count:]:
                print(line, flush=True)
            logged_count = len(state.log_lines)
            if state.phase != last_phase:
                print(f"[{state.current_item_name or config.title}] {state.phase.value}", flush=True)
                last_phase = state.phase
            time.sleep(_POLL_INTERVAL_SECONDS)
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        if solar_poller is not None:
            solar_poller.stop()

    final_state = runner.get_state()
    for line in final_state.log_lines[logged_count:]:
        print(line, flush=True)

    if stop_requested:
        print("stopped.", flush=True)
        return 130
    if final_state.phase == RunPhase.FAILED:
        print(f"FAILED: {final_state.error}", file=sys.stderr, flush=True)
        return 1
    print("done.", flush=True)
    return 0
