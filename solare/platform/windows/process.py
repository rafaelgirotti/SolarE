"""Process suspend/resume via NtSuspendProcess/NtResumeProcess.

Undocumented but extremely well-established - the same primitive Sysinternals `pssuspend.exe`
and Windows' own Task Manager "Suspend" action use. No elevation needed: opening a same-user
child process with just `PROCESS_SUSPEND_RESUME` is a narrow, ordinary access right, not a
security boundary crossing.

Both are `NTSTATUS NtXxxProcess(HANDLE)` - 0 is success, nonzero is failure; they don't raise on
access-denied, they return a nonzero code, which `suspend_process`/`resume_process` turn into a
bool. Only a PID is available here (not a process-creation handle from launching the process
directly), so `PROCESS_SUSPEND_RESUME` is opened fresh via `OpenProcess` each call.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

_ntdll = ctypes.WinDLL("ntdll")
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_PROCESS_SUSPEND_RESUME = 0x0800

_ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
_ntdll.NtSuspendProcess.restype = ctypes.c_long
_ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
_ntdll.NtResumeProcess.restype = ctypes.c_long

_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _with_handle(pid: int, ntdll_func) -> bool:
    handle = _kernel32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
    if not handle:
        return False
    try:
        return ntdll_func(handle) == 0
    finally:
        _kernel32.CloseHandle(handle)


def suspend_process(pid: int) -> bool:
    return _with_handle(pid, _ntdll.NtSuspendProcess)


def resume_process(pid: int) -> bool:
    return _with_handle(pid, _ntdll.NtResumeProcess)
