"""Common interface for OS-specific hardware access - see docs/ARCHITECTURE.md.

The rest of the codebase imports only from here, never reaches into
`solare.platform.windows`/`solare.platform.linux` directly - that keeps platform dispatch in one
place instead of scattered `if platform.system()` checks.
"""

from __future__ import annotations

import platform as _platform


def get_cpu_temp_power() -> tuple[float | None, float | None]:
    """Return (cpu_temp_c, cpu_power_w), or (None, None) if unavailable on this platform."""
    system = _platform.system()
    if system == "Windows":
        from solare.platform.windows.hwmonitor import get_cpu_temp_power as _impl

        return _impl()
    if system == "Linux":
        from solare.platform.linux.hwmonitor import get_cpu_temp_power as _impl

        return _impl()
    return None, None


def suspend_process(pid: int) -> bool:
    """Suspend a process at the kernel scheduler level. Returns False (not an exception) if
    unsupported on this platform or the call itself failed - callers should degrade gracefully,
    same as get_cpu_temp_power."""
    system = _platform.system()
    if system == "Windows":
        from solare.platform.windows.process import suspend_process as _impl

        return _impl(pid)
    if system == "Linux":
        from solare.platform.linux.process import suspend_process as _impl

        return _impl(pid)
    return False


def resume_process(pid: int) -> bool:
    system = _platform.system()
    if system == "Windows":
        from solare.platform.windows.process import resume_process as _impl

        return _impl(pid)
    if system == "Linux":
        from solare.platform.linux.process import resume_process as _impl

        return _impl(pid)
    return False


def subprocess_creation_flags() -> int:
    """Extra `subprocess.Popen(creationflags=...)` value for launching an external encoder tool -
    see the Windows implementation for why this matters there. 0 (a no-op) everywhere else."""
    system = _platform.system()
    if system == "Windows":
        from solare.platform.windows.process import subprocess_creation_flags as _impl

        return _impl()
    if system == "Linux":
        from solare.platform.linux.process import subprocess_creation_flags as _impl

        return _impl()
    return 0


def vapoursynth_dll_dir():
    """Return the directory containing the registered VapourSynth install's vsscript.dll, or
    None if unregistered or not applicable on this platform (Linux's dynamic linker finds
    VapourSynth through the normal system search path - no registry/PATH workaround needed)."""
    if _platform.system() == "Windows":
        from solare.platform.windows.vapoursynth import vapoursynth_dll_dir as _impl

        return _impl()
    return None
