"""CPU temperature/power via MSI Afterburner's MAHM shared memory.

Windows exposes no standard API for CPU temperature/power (SMBus/MSR access is vendor-specific
and not surfaced through any documented Win32 API). MSI Afterburner already has elevated driver
access and publishes its readings through a named shared-memory mapping that any unelevated
process can read - `MahmReader` opens that mapping directly rather than going through a heavier
hardware-monitoring library.

Struct layout confirmed against aleab/MSIAfterburnerNET's C# interop definitions: a 32-byte
header (8x uint32 - signature, version, headerSize, entryCount, entrySize, time, gpuEntryCount,
gpuEntrySize) followed by entryCount fixed-size entries, each a null-terminated ASCII name string
(260 bytes) followed by a float32 "data" value at offset 1300 within the entry.

Requires MSI Afterburner running. `get_cpu_temp_power()` returns (None, None), not (0, 0), if
Afterburner isn't running or the mapping can't be opened - callers should treat that as "unknown",
not "zero load."
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_FILE_MAP_READ = 0x0004

_kernel32.OpenFileMappingW.restype = wintypes.HANDLE
_kernel32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.MapViewOfFile.restype = ctypes.c_void_p
_kernel32.MapViewOfFile.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_size_t,
]
_kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


class MahmReader:
    _MAPPING_NAME = "MAHMSharedMemory"
    _NAME_FIELD_SIZE = 260
    _DATA_OFFSET_IN_ENTRY = 1300
    _HEADER_SIZE = 32

    def __init__(self) -> None:
        self._handle: int | None = None
        self._view: int | None = None

    def _ensure_mapped(self) -> bool:
        if self._view is not None:
            return True
        handle = _kernel32.OpenFileMappingW(_FILE_MAP_READ, False, self._MAPPING_NAME)
        if not handle:
            return False
        view = _kernel32.MapViewOfFile(handle, _FILE_MAP_READ, 0, 0, 0)
        if not view:
            _kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        self._view = view
        return True

    def _reset(self) -> None:
        if self._view:
            _kernel32.UnmapViewOfFile(self._view)
        if self._handle:
            _kernel32.CloseHandle(self._handle)
        self._handle = None
        self._view = None

    def read_value(self, name: str) -> float | None:
        if not self._ensure_mapped():
            return None
        try:
            header = (ctypes.c_char * self._HEADER_SIZE).from_address(self._view)
            header_size, entry_count, entry_size = struct.unpack_from("<III", header, 8)
            total_size = header_size + entry_count * entry_size
            buf = (ctypes.c_char * total_size).from_address(self._view)
            for i in range(entry_count):
                base = header_size + i * entry_size
                raw_name = bytes(buf[base : base + self._NAME_FIELD_SIZE])
                entry_name = raw_name.split(b"\x00", 1)[0].decode("ascii", errors="replace")
                if entry_name == name:
                    (value,) = struct.unpack_from("<f", buf, base + self._DATA_OFFSET_IN_ENTRY)
                    return round(value, 1)
            return None
        except (OSError, ValueError, struct.error):
            self._reset()
            return None

    def read_cpu_temp_power(self) -> tuple[float | None, float | None]:
        return self.read_value("CPU temperature"), self.read_value("CPU power")

    def close(self) -> None:
        self._reset()


_reader: MahmReader | None = None


def get_cpu_temp_power() -> tuple[float | None, float | None]:
    global _reader
    if _reader is None:
        _reader = MahmReader()
    return _reader.read_cpu_temp_power()
