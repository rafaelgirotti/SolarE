"""Shared color constants and threshold-based gradients for dashboard markup.

Colors here are generic hardware-safety bands (not tuned to any one machine) - reasonable
defaults for "this is fine" / "keep an eye on it" / "this is a real problem" on typical consumer
CPUs/GPUs. Deliberately NOT applied to load percentage or power draw: during a batch encode those
are expected to sit near their max as a sign the job is working correctly, not a danger signal -
coloring them red at high values would just make the dashboard alarm constantly during normal
operation, which trains you to ignore it.
"""

from __future__ import annotations

SAFE = "#33cc33"
WARN = "#ffb000"
DANGER = "#ff3333"
UNKNOWN = "#4a5a4a"
PHOSPHOR = "#55ff55"

CPU_TEMP_WARN_C = 70.0
CPU_TEMP_DANGER_C = 85.0
GPU_TEMP_WARN_C = 75.0
GPU_TEMP_DANGER_C = 85.0
RAM_LOAD_WARN_PCT = 70.0
RAM_LOAD_DANGER_PCT = 90.0
DISK_FREE_WARN_GB = 50.0
DISK_FREE_DANGER_GB = 10.0

COLD = "#4da6ff"
WEATHER_COLD_BELOW_C = 15.0
WEATHER_HOT_ABOVE_C = 35.0


def rising_gradient(value: float | None, warn_at: float, danger_at: float) -> str:
    """Color for a value where HIGHER is riskier (temperature, RAM load)."""
    if value is None:
        return UNKNOWN
    if value >= danger_at:
        return DANGER
    if value >= warn_at:
        return WARN
    return SAFE


def weather_color(temp_c: float) -> str | None:
    """Blue when cold, warm-tinted above a heat threshold (panel efficiency drops in heat),
    None (default text color) for ordinary mild weather - a temperature convention, not a risk
    gradient, so it uses its own hue rather than SAFE/WARN/DANGER."""
    if temp_c < WEATHER_COLD_BELOW_C:
        return COLD
    if temp_c > WEATHER_HOT_ABOVE_C:
        return WARN
    return None


def falling_gradient(value: float | None, warn_below: float, danger_below: float) -> str:
    """Color for a value where LOWER is riskier (free disk space)."""
    if value is None:
        return UNKNOWN
    if value <= danger_below:
        return DANGER
    if value <= warn_below:
        return WARN
    return SAFE
