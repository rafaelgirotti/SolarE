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
