"""CPU temperature/power on Linux.

Temperature comes from `psutil.sensors_temperatures()` (a thin wrapper over the kernel's own
hwmon sysfs tree - no lm-sensors userspace package required, just whichever driver already
exposes the chip). Which sensor is "the" CPU package temperature is vendor-specific, so a short
list of known chip/label combinations is tried in order (AMD's `k10temp`/`zenpower`, Intel's
`coretemp`) - confirmed against a real k10temp-equipped machine, not guessed.

Power comes from Linux's RAPL energy counters via `/sys/class/powercap/intel-rapl:*` (the
"intel-rapl" powercap class name is a historical/framework artifact - AMD Zen CPUs expose
RAPL-compatible MSRs through the same sysfs class, confirmed live on a Ryzen machine with no
Intel hardware involved at all). Unlike temperature, this file is root-only by default on a
stock Fedora kernel (`-r--------`, confirmed live) - a known hardening against RAPL-based side-
channel attacks (CVE-2020-8694 and related). Rather than requiring solare to run as root, this
degrades to `None` (same "unknown, not zero" contract as Windows' Afterburner-not-running case)
if the file can't be read. A user who wants real wattage can relax that permission themselves
(e.g. a udev rule making the package zone's energy_uj group-readable) - not done automatically
here since it's a real (if narrow) local security tradeoff for the user to opt into, not this
tool's call to make silently.
"""

from __future__ import annotations

import time
from pathlib import Path

import psutil

_POWERCAP_ROOT = Path("/sys/class/powercap")

# (chip name as psutil/hwmon reports it, label substrings to try in order) - first chip that's
# present wins, then first label match within it. Tccd1/Tdie are per-chiplet/die on multi-CCD
# AMD parts, kept as fallbacks behind the true package control temperature (Tctl).
_CPU_TEMP_CANDIDATES = [
    ("k10temp", ["Tctl", "Tdie", "Tccd1"]),
    ("zenpower", ["Tdie", "Tctl"]),
    ("coretemp", ["Package id 0", "Package id 1"]),
]


def _find_cpu_temp() -> float | None:
    try:
        all_temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    for chip, labels in _CPU_TEMP_CANDIDATES:
        entries = all_temps.get(chip)
        if not entries:
            continue
        for label in labels:
            for entry in entries:
                if entry.label == label:
                    return round(entry.current, 1)
        # Chip present but none of the known labels matched (a driver version/naming quirk) -
        # fall back to its first reading rather than reporting nothing for a real CPU sensor.
        return round(entries[0].current, 1)
    return None


def _find_rapl_package_zone() -> Path | None:
    if not _POWERCAP_ROOT.is_dir():
        return None
    for zone in sorted(_POWERCAP_ROOT.glob("intel-rapl:*")):
        if ":" in zone.name.removeprefix("intel-rapl:"):
            continue  # a subzone (e.g. intel-rapl:0:0 "core") - only the top-level package zone
        try:
            if zone.joinpath("name").read_text().strip().startswith("package-"):
                return zone
        except OSError:
            continue
    return None


class _RaplPowerReader:
    """Package power via two energy_uj samples - RAPL exposes cumulative microjoules, not an
    instantaneous watt reading, so power is derived from a delta over real elapsed time, same
    "needs at least two polls" shape as psutil.cpu_percent() already has elsewhere in this
    codebase. Handles the counter wrapping back to 0 past max_energy_range_uj."""

    def __init__(self) -> None:
        self._zone = _find_rapl_package_zone()
        self._energy_path = self._zone / "energy_uj" if self._zone is not None else None
        self._max_range_uj: int | None = None
        if self._zone is not None:
            try:
                self._max_range_uj = int(self._zone.joinpath("max_energy_range_uj").read_text())
            except (OSError, ValueError):
                self._max_range_uj = None
        self._unreadable = False  # sticky - a permission error isn't going to fix itself mid-run,
        # no point retrying every single poll for the rest of the process' life
        self._prev_energy_uj: int | None = None
        self._prev_time: float | None = None

    def read_power_w(self) -> float | None:
        if self._energy_path is None or self._unreadable:
            return None
        try:
            energy_uj = int(self._energy_path.read_text())
        except (OSError, ValueError):
            self._unreadable = True
            return None
        now = time.monotonic()
        power_w = None
        if self._prev_energy_uj is not None and self._prev_time is not None:
            delta_uj = energy_uj - self._prev_energy_uj
            if delta_uj < 0 and self._max_range_uj is not None:  # counter wrapped
                delta_uj += self._max_range_uj
            delta_t = now - self._prev_time
            if delta_uj >= 0 and delta_t > 0:
                power_w = round(delta_uj / 1_000_000 / delta_t, 1)
        self._prev_energy_uj = energy_uj
        self._prev_time = now
        return power_w


_rapl_reader: _RaplPowerReader | None = None


def get_cpu_temp_power() -> tuple[float | None, float | None]:
    global _rapl_reader
    if _rapl_reader is None:
        _rapl_reader = _RaplPowerReader()
    return _find_cpu_temp(), _rapl_reader.read_power_w()
