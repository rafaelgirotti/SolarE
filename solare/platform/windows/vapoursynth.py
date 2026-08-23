"""Locate the registered VapourSynth install via its own registry entries.

VapourSynth's Windows installer (winget's VapourSynth.VapourSynth package) writes real registry
keys under HKCU\\SOFTWARE\\VapourSynth - this is how av1an's own bare LoadLibrary call is meant to
find vsscript.dll. Verified directly: the registry entry alone isn't sufficient - av1an still
fails ("cannot open shared object file") unless vsscript.dll's own directory is also explicitly on
PATH, which callers must add themselves (see engine/toolpath.py).
"""

from __future__ import annotations

import winreg
from pathlib import Path


def vapoursynth_dll_dir() -> Path | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\VapourSynth") as key:
            dll_path, _ = winreg.QueryValueEx(key, "VSScriptDLL")
    except OSError:
        return None
    return Path(dll_path).parent
