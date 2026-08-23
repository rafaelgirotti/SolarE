"""Cross-platform CPU/GPU/RAM load, temperature, and power snapshots.

CPU/RAM load comes from `psutil` (works on both platforms). CPU temperature/power comes from
`solare.platform.get_cpu_temp_power()` - platform-specific, see docs/ARCHITECTURE.md for why.
NVIDIA GPU stats come from `pynvml`, optional - the `gpu_*` fields stay `None` if it's not
installed or no NVIDIA GPU is present.
"""

from __future__ import annotations

from dataclasses import dataclass

import psutil

from solare import platform as solare_platform

try:
    import pynvml

    _NVML_AVAILABLE = True
except ImportError:
    _NVML_AVAILABLE = False


@dataclass
class HardwareSnapshot:
    cpu_total_load_pct: float
    cpu_max_core_load_pct: float
    cpu_temp_c: float | None
    cpu_power_w: float | None
    ram_used_gb: float
    ram_load_pct: float
    gpu_temp_c: float | None = None
    gpu_load_pct: float | None = None
    gpu_power_w: float | None = None
    gpu_mem_used_mb: float | None = None
    gpu_mem_total_mb: float | None = None


class HardwareMonitor:
    def __init__(self) -> None:
        self._nvml_handle = None
        if _NVML_AVAILABLE:
            try:
                pynvml.nvmlInit()
                self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            except pynvml.NVMLError:
                self._nvml_handle = None

    def poll(self) -> HardwareSnapshot:
        """Take one snapshot. `psutil.cpu_percent`'s first-ever call always returns 0 for every
        core (it measures load between calls) - poll at least twice before trusting CPU numbers.
        """
        per_core = psutil.cpu_percent(percpu=True)
        cpu_total = sum(per_core) / len(per_core) if per_core else 0.0
        cpu_max = max(per_core) if per_core else 0.0

        mem = psutil.virtual_memory()
        ram_used_gb = round((mem.total - mem.available) / (1024**3), 2)

        cpu_temp_c, cpu_power_w = solare_platform.get_cpu_temp_power()

        gpu_temp_c = gpu_load_pct = gpu_power_w = gpu_mem_used_mb = gpu_mem_total_mb = None
        if self._nvml_handle is not None:
            try:
                gpu_temp_c = pynvml.nvmlDeviceGetTemperature(
                    self._nvml_handle, pynvml.NVML_TEMPERATURE_GPU
                )
                gpu_load_pct = pynvml.nvmlDeviceGetUtilizationRates(self._nvml_handle).gpu
                gpu_power_w = round(pynvml.nvmlDeviceGetPowerUsage(self._nvml_handle) / 1000, 1)
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(self._nvml_handle)
                gpu_mem_used_mb = round(mem_info.used / (1024**2), 1)
                gpu_mem_total_mb = round(mem_info.total / (1024**2), 1)
            except pynvml.NVMLError:
                pass

        return HardwareSnapshot(
            cpu_total_load_pct=round(cpu_total, 1),
            cpu_max_core_load_pct=round(cpu_max, 1),
            cpu_temp_c=cpu_temp_c,
            cpu_power_w=cpu_power_w,
            ram_used_gb=ram_used_gb,
            ram_load_pct=round(mem.percent, 1),
            gpu_temp_c=gpu_temp_c,
            gpu_load_pct=gpu_load_pct,
            gpu_power_w=gpu_power_w,
            gpu_mem_used_mb=gpu_mem_used_mb,
            gpu_mem_total_mb=gpu_mem_total_mb,
        )

    def close(self) -> None:
        if self._nvml_handle is not None:
            try:
                pynvml.nvmlShutdown()
            except pynvml.NVMLError:
                pass
