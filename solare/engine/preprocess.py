"""Generates a VapourSynth preprocessing script (deinterlace and/or speed correction) that av1an
consumes directly as its own -i input - confirmed directly against av1an's own --help ("Can be a
video or VapourSynth (.py, .vpy) script"), so chunking/encoding reads straight off the filtered
output with no separate full-file transcode pass and no intermediate file written to disk.

QTGMC (havsfunc) needs a real dependency chain installed into the system VapourSynth (not
solare's own uv-managed venv, which never imports vapoursynth directly - see toolpath.py):
`vsrepo install havsfunc mvsfunc mv rgvs nnedi3 nnedi3_resample nnedi3_weights fmtc znedi3`, plus
`pip install vsutil` (havsfunc's one pure-Python dependency, not a vsrepo package) into that same
system Python. See the README's Requirements section.
"""

from __future__ import annotations

from pathlib import Path

from solare.engine.config import TitleConfig

_LOADERS = {
    "bestsource": "core.bs.VideoSource",
    "lsmash": "core.lsmas.LWLibavSource",
    "ffms2": "core.ffms2.Source",
}


def needs_preprocessing(config: TitleConfig) -> bool:
    return config.video.deinterlace is not None or config.video.speed_correction is not None


def generate_vpy(config: TitleConfig, src_file: Path, out_vpy: Path, chunk_method: str) -> Path:
    """Write a .vpy script that loads src_file through the same underlying VapourSynth source
    plugin the configured chunk method would otherwise use directly, then applies deinterlacing
    and/or speed correction as configured. Raises ValueError for a chunk method with no
    VapourSynth-plugin loader (hybrid/select/segment/dgdecnv) - preprocessing needs one."""
    loader = _LOADERS.get(chunk_method)
    if loader is None:
        raise ValueError(
            f"preprocessing requires a VapourSynth-plugin-based chunk method "
            f"(one of {sorted(_LOADERS)}), got {chunk_method!r}"
        )

    video = config.video
    lines = ["import vapoursynth as vs", "core = vs.core", "", f'clip = {loader}(r"{src_file}")']

    if video.deinterlace is not None:
        d = video.deinterlace
        lines.append("import havsfunc")
        field_based = 2 if d.tff else 1  # VapourSynth _FieldBased: 1=BFF, 2=TFF
        lines.append(f"clip = core.std.SetFieldBased(clip, {field_based})")
        kwargs = ", ".join(f"{k}={v!r}" for k, v in d.params.items())
        extra = f", {kwargs}" if kwargs else ""
        lines.append(f"clip = havsfunc.QTGMC(clip, TFF={d.tff}, FPSDivisor={d.fps_divisor}{extra})")

    if video.speed_correction is not None:
        num, den = _fps_to_fraction(video.speed_correction.target_fps)
        lines.append(f"clip = core.std.AssumeFPS(clip, fpsnum={num}, fpsden={den})")

    lines.append("clip.set_output()")

    out_vpy.write_text("\n".join(lines) + "\n")
    return out_vpy


def _fps_to_fraction(fps: str) -> tuple[int, int]:
    if "/" in fps:
        num, den = fps.split("/", 1)
        return int(num), int(den)
    return int(round(float(fps) * 1000)), 1000
