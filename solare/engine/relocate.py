"""Moves an entire job's temp/output directory tree to a new parent location, rewriting every
absolute-path reference av1an embedded into its own chunks.json so a subsequent -r resume still
works there.

av1an hardcodes the --temp path and the preprocessing script's path into every chunk entry at
generation time - both as plain JSON strings (`temp`, `input.VapourSynth.path`,
`target_quality.temp`) AND, separately, as the OS argv byte array it replays to invoke vspipe for
that chunk (`source_cmd`/`proxy_cmd`, each argument encoded as `{"Windows": [byte, byte, ...]}`).
A plain text find-and-replace over the file would silently miss the byte-array form - confirmed
directly against a real chunks.json, not assumed: every one of these fields, across every chunk
entry, needed decoding before the embedded path was visible as text at all.

Only useful when the enclosing directory is what's moving - every file/folder name nested inside
stays identical, so this is a path-*prefix* substitution, not a general rename, and only pays off
when there's real progress worth preserving (the whole point is avoiding a from-scratch restart).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path


def relocate_job_dir(old_dir: Path, new_dir: Path, temp_dir_name: str) -> None:
    """Moves `old_dir` to `new_dir` (which must not already exist) and rewrites the embedded
    paths in `<new_dir>/<temp_dir_name>/chunks.json` accordingly. Raises FileNotFoundError if
    chunks.json doesn't exist - nothing to rewrite means this isn't the right tool (an av1an temp
    dir that never got this far has no embedded paths to fix, and generating it fresh at the new
    location, e.g. by just starting a normal encode there, does the same job without the risk)."""
    old_dir = old_dir.resolve()
    new_dir = new_dir.resolve()
    old_prefix = str(old_dir)
    new_prefix = str(new_dir)

    chunks_path = old_dir / temp_dir_name / "chunks.json"
    if not chunks_path.is_file():
        raise FileNotFoundError(f"No chunks.json found at {chunks_path} - nothing to relocate")

    new_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_dir), str(new_dir))

    chunks_path = new_dir / temp_dir_name / "chunks.json"
    data = json.loads(chunks_path.read_text())
    for entry in data:
        entry["temp"] = _replace_prefix(entry["temp"], old_prefix, new_prefix)
        vs = entry.get("input", {}).get("VapourSynth")
        if vs is not None:
            vs["path"] = _replace_prefix(vs["path"], old_prefix, new_prefix)
        target_quality = entry.get("target_quality")
        if target_quality is not None and "temp" in target_quality:
            target_quality["temp"] = _replace_prefix(target_quality["temp"], old_prefix, new_prefix)
        for cmd_field in ("source_cmd", "proxy_cmd"):
            cmd = entry.get(cmd_field)
            if cmd:
                entry[cmd_field] = [_replace_cmd_arg(arg, old_prefix, new_prefix) for arg in cmd]
    chunks_path.write_text(json.dumps(data))


def _replace_prefix(value: str, old_prefix: str, new_prefix: str) -> str:
    if value.startswith(old_prefix):
        return new_prefix + value[len(old_prefix) :]
    return value


def _replace_cmd_arg(item: dict, old_prefix: str, new_prefix: str) -> dict:
    if "Windows" not in item:
        return item
    decoded = bytes(item["Windows"]).decode("utf-8")
    replaced = _replace_prefix(decoded, old_prefix, new_prefix)
    if replaced == decoded:
        return item
    return {"Windows": list(replaced.encode("utf-8"))}
