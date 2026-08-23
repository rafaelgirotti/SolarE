"""CPU temperature/power on Linux - not yet implemented.

Windows reads these from MSI Afterburner's MAHM shared memory (see
`solare.platform.windows.hwmonitor`), which has no Linux equivalent. The intended source here is
`psutil.sensors_temperatures()` (backed by lm-sensors) for temperature; package power has no
equally standard cross-vendor source on Linux (RAPL via `powercap`/`turbostat` is the usual route,
but needs a real Linux machine to verify against rather than guessing). Lands once development
moves to Linux.
"""

from __future__ import annotations


def get_cpu_temp_power() -> tuple[float | None, float | None]:
    return None, None
