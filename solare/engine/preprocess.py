"""Generates a VapourSynth preprocessing script (deinterlace, speed correction, and/or AI upscale)
that av1an consumes directly as its own -i input - confirmed directly against av1an's own --help
("Can be a video or VapourSynth (.py, .vpy) script"), so chunking/encoding reads straight off the
filtered output with no separate full-file transcode pass and no intermediate file written to disk.

QTGMC (havsfunc) needs a real dependency chain installed into the system VapourSynth (not
solare's own uv-managed venv, which never imports vapoursynth directly - see toolpath.py):
`vsrepo install havsfunc mvsfunc mv rgvs nnedi3 nnedi3_resample nnedi3_weights fmtc znedi3`, plus
`pip install vsutil` (havsfunc's one pure-Python dependency, not a vsrepo package) into that same
system Python. See the README's Requirements section.

Upscale (vs-mlrt's TensorRT `vstrt` backend) needs `vstrt.dll`/`vsncnn.dll` and the model .onnx
files already placed inside that same system VapourSynth's own plugins directory (same
non-PATH-based reason QTGMC's plugins need `vsrepo`, not toolpath.py's PATH-prepend), plus a
pre-built TensorRT engine for the exact post-crop resolution in use - see ../../../tools/vsmlrt.md
and this module's engine_path()/_require_upscale_engine().
"""

from __future__ import annotations

from pathlib import Path

from solare.engine.config import TitleConfig, UpscaleSettings
from solare.engine.toolpath import shared_tools_root

_LOADERS = {
    "bestsource": "core.bs.VideoSource",
    "lsmash": "core.lsmas.LWLibavSource",
    "ffms2": "core.ffms2.Source",
}


def needs_preprocessing(config: TitleConfig) -> bool:
    return (
        config.video.deinterlace is not None
        or config.video.speed_correction is not None
        or config.video.upscale is not None
    )


def supports_index_cache(chunk_method: str) -> bool:
    """Whether `chunk_method` has a VapourSynth-plugin loader this module knows how to redirect
    the chunk-index cache for (see `_cache_kwarg`) - used by av1an.py to decide whether it's worth
    generating a passthrough .vpy (no real filtering, just the loader + cachedir) purely to keep
    that index file out of the source folder even for a title with no real preprocessing."""
    return chunk_method in _LOADERS


def _parse_crop(crop: str) -> tuple[int, int, int, int]:
    """`video.crop` is the same "w:h:x:y" shape ffmpeg's own `-vf crop=` filter takes (that's
    where it normally goes - see av1an.py's build_args()) - reused as-is here rather than
    inventing a second crop format, since it means the same value."""
    w, h, x, y = (int(v) for v in crop.split(":"))
    return w, h, x, y


def engine_path(model: str, width: int, height: int) -> Path:
    """Where a TensorRT engine for `model` at this exact post-crop resolution is expected to
    live - a fixed convention, not something read from config (keeps the JSON free of per-machine
    absolute paths, matching every other shared-tools reference in this codebase)."""
    return shared_tools_root() / "vsmlrt" / "vstrt" / f"{model}_{width}x{height}.engine"


def _require_upscale_engine(upscale: UpscaleSettings, width: int, height: int) -> Path:
    path = engine_path(upscale.model, width, height)
    if not path.is_file():
        onnx = shared_tools_root() / "vsmlrt" / "models" / "RealESRGANv2" / f"{upscale.model}.onnx"
        raise FileNotFoundError(
            f"upscale engine not found: {path}\n"
            f"Build it once via trtexec (see tools/vsmlrt.md):\n"
            f"  trtexec --onnx={onnx} --saveEngine={path} --fp16 "
            f"--minShapes=input:1x3x{height}x{width} "
            f"--optShapes=input:1x3x{height}x{width} "
            f"--maxShapes=input:1x3x{height}x{width}"
        )
    return path


def _cache_kwarg(chunk_method: str, cache_dir: Path, src_file: Path) -> str:
    """Keeps each source plugin's own index cache (av1an's chunking otherwise defaults to writing
    it next to src_file - e.g. lsmash's <name>.lwi - littering the source folder) inside the same
    output-side directory as everything else this run generates. Each plugin has a differently
    named/shaped parameter for this - confirmed directly against each plugin's own real
    .signature(), not guessed - though only lsmash's has been verified end to end against a real
    index build; bestsource/ffms2 are implemented from their documented signatures.

    lsmash's `cachedir` (a bare directory) makes the plugin invent its own cache filename by
    flattening the *entire source path* into one string (every `\\`/`:` replaced with `_`) - no
    length guard. Confirmed live on Ghost in the Shell: SAC_2045 (a long bracketed release-folder
    name under an already-long output path): the flattened name landed at 312 characters, past
    Windows' 260-char MAX_PATH, and lsmash failed with "unable to create index file" - av1an
    exits with no useful log at all, just a raw VapourSynth traceback on stderr. `cachefile` (an
    exact path, not just a directory) sidesteps the auto-flattening entirely - same fix already
    used for ffms2 below. Verified: same source now produces a 191-character cache path."""
    if chunk_method == "lsmash":
        return f', cachefile=r"{cache_dir / (src_file.stem + ".lwi")}"'
    if chunk_method == "bestsource":
        return f', cachepath=r"{cache_dir}"'
    if chunk_method == "ffms2":
        return f', cachefile=r"{cache_dir / (src_file.name + ".ffindex")}"'
    return ""


def generate_vpy(config: TitleConfig, src_file: Path, out_vpy: Path, chunk_method: str) -> Path:
    """Write a .vpy script that loads src_file through the same underlying VapourSynth source
    plugin the configured chunk method would otherwise use directly, then applies (in this fixed
    order) crop+upscale, deinterlacing, and speed correction, as configured. Raises ValueError for
    a chunk method with no VapourSynth-plugin loader (hybrid/select/segment/dgdecnv) - preprocessing
    needs one.

    Upscale runs first, ahead of deinterlace, because crop+upscale's whole reason for living in
    this script is TensorRT's fixed-shape engine (see engine_path()) - nothing else here has that
    constraint. No title combines deinterlace with upscale yet, so this ordering is untested for
    that combination and is real wasted work if it ever happens (QTGMC would run on the already-4x
    -larger frame) - worth revisiting (deinterlace before upscale instead) if that combination is
    ever actually needed, not preemptively solved here."""
    lines = _build_vpy_lines(config, src_file, out_vpy, chunk_method, include_upscale=True)
    out_vpy.write_text("\n".join(lines) + "\n")
    return out_vpy


def generate_proxy_vpy(config: TitleConfig, src_file: Path, out_vpy: Path, chunk_method: str) -> Path:
    """A cheaper scene-detection-only stand-in for av1an's own `--proxy` flag - mirrors
    generate_vpy()'s pipeline exactly (same loader, same crop, same deinterlace/speed-correction
    if those are ever combined with upscale) *except* it skips the upscale filter itself.

    Why this exists: av1an's own scene-detection pass (finding chunk-split boundaries) decodes
    the *entire* source through whatever script it's given as input - with no upscale-aware proxy,
    that means running the full TensorRT upscale on all 179k+ frames of a feature-length film
    purely to look at frame differences, then throwing the result away, before encoding runs the
    exact same upscale *again* per chunk for real. Confirmed live: this made scene-detection alone
    take longer than the isolated upscale-only benchmark suggested, on top of being a real ~2x
    upscale-compute waste. `--proxy`'s only hard requirement (per `av1an --help`) is producing the
    *same frame count* as the real input - true here by construction, since skipping the upscale
    filter doesn't change frame count, only resolution.

    Only meaningful (and only ever called) when `video.upscale` is set - a title with no upscale
    has nothing expensive for scene-detection to skip in the first place."""
    lines = _build_vpy_lines(config, src_file, out_vpy, chunk_method, include_upscale=False)
    out_vpy.write_text("\n".join(lines) + "\n")
    return out_vpy


def _build_vpy_lines(
    config: TitleConfig, src_file: Path, out_vpy: Path, chunk_method: str, include_upscale: bool
) -> list[str]:
    loader = _LOADERS.get(chunk_method)
    if loader is None:
        raise ValueError(
            f"preprocessing requires a VapourSynth-plugin-based chunk method "
            f"(one of {sorted(_LOADERS)}), got {chunk_method!r}"
        )

    video = config.video
    cache_kwarg = _cache_kwarg(chunk_method, out_vpy.parent, src_file)
    lines = [
        "import vapoursynth as vs",
        "core = vs.core",
        "",
        f'clip = {loader}(r"{src_file}"{cache_kwarg})',
    ]

    if video.upscale is not None:
        # Crop moves into the script (applied here, ahead of everything else) only in this branch
        # - av1an.py's build_args() skips its own ffmpeg-side `-vf crop=` flag whenever upscale is
        # set, specifically to avoid double-cropping. Every other title keeps today's ffmpeg-side
        # crop untouched. The TensorRT engine needs the *cropped* frame: it's a fixed-shape
        # compiled artifact built for one exact resolution (see engine_path()) - upscale requires
        # `crop` to be set for exactly this reason, checked here rather than left to a confusing
        # failure inside vs-mlrt/TensorRT later. Applied identically in the proxy script (whether
        # or not include_upscale is True) - crop doesn't affect frame count so it isn't required
        # for the proxy's own correctness, but keeping it identical is simple and harmless.
        if not video.crop:
            raise ValueError(
                "video.upscale requires video.crop to be set - the TensorRT engine is built for "
                "one exact post-crop resolution, so it can't be inferred from the raw source."
            )
        w, h, x, y = _parse_crop(video.crop)
        lines.append(f"clip = core.std.CropAbs(clip, width={w}, height={h}, left={x}, top={y})")

        if include_upscale:
            path = _require_upscale_engine(video.upscale, w, h)
            lines.append('clip = core.resize.Bicubic(clip, format=vs.RGBS, matrix_in_s="709")')
            lines.append(
                f'clip = core.trt.Model(clip, engine_path=r"{path}", '
                f"use_cuda_graph={video.upscale.use_cuda_graph})"
            )
            # Deliberately back to plain 8-bit YUV, not video.pix_fmt directly - av1an's own
            # `--pix-format` flag (already set from video.pix_fmt in av1an.py's build_args(),
            # applied regardless of what this script outputs) handles the final bit-depth
            # conversion, exactly as it already does for every other title whether or not this
            # script runs at all.
            lines.append('clip = core.resize.Bicubic(clip, format=vs.YUV420P8, matrix_s="709")')

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
    return lines


def _fps_to_fraction(fps: str) -> tuple[int, int]:
    if "/" in fps:
        num, den = fps.split("/", 1)
        return int(num), int(den)
    return int(round(float(fps) * 1000)), 1000
