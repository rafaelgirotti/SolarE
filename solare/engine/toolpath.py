"""Local encoder tool discovery - see the README's Requirements section.

Checks two places for each tool, in order, before falling back to whatever's already on PATH:

1. This project's own `tools/<name>/` (gitignored, never committed) - an intentional per-project
   override/pin, e.g. if solare ever needs a different build/version than other projects share.
2. `D:\\Workspaces\\tools\\<name>\\` (or wherever this project's own parent workspace directory
   is - not hardcoded) - shared across every project under the same workspace, so ffmpeg/av1an/
   etc. don't need a separate multi-hundred-MB copy per project (solare and img-enc were both
   independently vendoring the same ffmpeg build before this). See that directory's own README
   for the convention.

This is pure convenience either way - if a tool isn't found in either location, whatever's
already on PATH is used unchanged.

VapourSynth is deliberately NOT in this list. Verified directly: a byte-for-byte copy of a working
VapourSynth install, relocated to a plain tools/vapoursynth/ folder, fails ("Failed to get
VSScript API") even though every DLL is present and identical - its loader depends on being
discovered through a real install (confirmed working via winget's VapourSynth.VapourSynth package,
which writes proper registry entries under HKCU/SOFTWARE/VapourSynth). Install it - and its
chunking plugins (L-SMASH/FFMS2/BestSource) - per av1an's own installation instructions
(https://github.com/rust-av/Av1an#installation), not by copying files here.

The registry entry alone still isn't enough, though: av1an's own bare LoadLibrary call to
vsscript.dll fails with "cannot open shared object file" unless that DLL's directory is also on
PATH. This function adds it (via solare.platform.vapoursynth_dll_dir()) for exactly that reason.
"""

from __future__ import annotations

import os
from pathlib import Path

from solare import platform as solare_platform

_TOOL_SUBDIRS = ["av1an", "ffmpeg", "x265", "svt-av1", "mkvtoolnix", "dovi_tool", "cuda-tensorrt"]
# "vsmlrt" is deliberately NOT in this list, same reasoning as VapourSynth itself below: vstrt.dll/
# vsncnn.dll and the model .onnx files must live inside VapourSynth's own registered plugins
# directory (VapourSynth only autoloads from there, not from PATH) - a PATH-prepend can't make
# that happen. See tools/vsmlrt.md / tools/README.md for that manual setup. "cuda-tensorrt" *is*
# PATH-based (trtexec and the TensorRT runtime DLLs are found via PATH, confirmed directly) - only
# vsmlrt's own plugin files need the different, non-PATH mechanism.


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def shared_tools_root() -> Path:
    """The workspace-level shared tools directory, a sibling of this project itself - computed
    relative to _project_root()'s own parent rather than a hardcoded drive/username, so this
    still resolves correctly if the whole workspace ever moves."""
    return _project_root().parent / "tools"


def prepend_local_tools_to_path() -> list[Path]:
    """Prepend any existing tools/<name>/ directories - this project's own first (an intentional
    per-project override/pin), the shared workspace-level one otherwise - plus the registered
    VapourSynth install's own directory if found, to PATH. Returns the ones actually found, for
    logging - call once, early, before launching any external tool."""
    found = []
    for name in _TOOL_SUBDIRS:
        for root in (_project_root() / "tools", shared_tools_root()):
            candidate = root / name
            if candidate.is_dir():
                found.append(candidate)
                break  # project-local override wins over the shared default, not both
    vs_dir = solare_platform.vapoursynth_dll_dir()
    if vs_dir is not None:
        found.append(vs_dir)
    if found:
        os.environ["PATH"] = os.pathsep.join(str(p) for p in found) + os.pathsep + os.environ["PATH"]
    return found
