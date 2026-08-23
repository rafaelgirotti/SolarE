"""Local encoder tool discovery - see the README's Requirements section.

If a `tools/<name>/` directory exists next to the project (gitignored, never committed - large,
platform-specific, often GPL-licensed binaries don't belong in this repo), its path is prepended
to PATH so `av1an`/`ffmpeg`/etc. resolve without a global PATH change. This is pure convenience -
if `tools/` doesn't exist, or a given subdirectory isn't there, whatever's already on PATH is
used unchanged.

VapourSynth is deliberately NOT in this list. Verified directly: a byte-for-byte copy of a working
VapourSynth install, relocated to a plain tools/vapoursynth/ folder, fails ("Failed to get
VSScript API") even though every DLL is present and identical - its loader depends on being
discovered through a real install (confirmed working via winget's VapourSynth.VapourSynth package,
which writes proper HKCU\SOFTWARE\VapourSynth registry entries), not just being on PATH. Install it
- and its chunking plugins (L-SMASH/FFMS2/BestSource) - per av1an's own installation instructions
(https://github.com/rust-av/Av1an#installation), not by copying files here.
"""

from __future__ import annotations

import os
from pathlib import Path

_TOOL_SUBDIRS = ["av1an", "ffmpeg", "x265", "svt-av1", "mkvtoolnix", "dovi_tool"]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def prepend_local_tools_to_path() -> list[Path]:
    """Prepend any existing tools/<name>/ directories to PATH. Returns the ones actually found,
    for logging - call once, early, before launching any external tool."""
    tools_root = _project_root() / "tools"
    found = [tools_root / name for name in _TOOL_SUBDIRS if (tools_root / name).is_dir()]
    if found:
        os.environ["PATH"] = os.pathsep.join(str(p) for p in found) + os.pathsep + os.environ["PATH"]
    return found
