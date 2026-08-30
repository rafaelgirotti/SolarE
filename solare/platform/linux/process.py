"""Process suspend/resume via SIGSTOP/SIGCONT - the standard POSIX equivalent of Windows'
NtSuspendProcess/NtResumeProcess (see the Windows implementation for why that primitive is used
there). Freezes/resumes at the kernel scheduler level either way; no elevation needed for a
same-user child process.
"""

from __future__ import annotations

import os
import signal
import sys


def suspend_process(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGSTOP)
        return True
    except OSError:
        return False


def resume_process(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGCONT)
        return True
    except OSError:
        return False


def subprocess_creation_flags() -> int:
    """No Windows-style shared-console-title hijacking risk on Linux - a child process here has
    no equivalent way to reach back and rewrite the parent terminal's title bar unprompted."""
    return 0


def set_console_title(title: str) -> None:
    """The standard xterm OSC 0 escape sequence, understood by every terminal emulator that
    matters (the same VT/ANSI processing Textual itself already relies on to render at all)."""
    sys.stdout.write(f"\x1b]0;{title}\x07")
    sys.stdout.flush()
