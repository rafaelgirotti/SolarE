"""Final mux: video + audio tracks + subtitles + chapters + font attachments, clean metadata.

Video and audio are always stream-copied here - both were already produced by earlier passes
(encode, and transcode-to-Opus respectively). Subtitles and chapters come from the source file by
default, or from a standalone external file for a subtitle sourced outside it (e.g. a release with
no subtitle track at all in the language needed, filled in from a separate project's files).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from solare.engine.config import Subtitle, TitleConfig
from solare.engine.ffprobe import find_stream_index
from solare.engine.queue import clean_title


@dataclass
class SubtitleSource:
    subtitle: Subtitle
    stream_index: int | None  # set when subtitle.source == "primary"
    external_path: Path | None  # set when subtitle.source == "external"


def resolve_subtitle_sources(
    config: TitleConfig, src_file: Path, episode_tag: str | None
) -> list[SubtitleSource]:
    sources = []
    for sub in config.subtitles:
        if sub.source == "external":
            if not sub.external_pattern:
                raise ValueError(f"Subtitle {sub.title!r} has source=external but no externalPattern")
            path = Path(sub.external_pattern.replace("{EP}", episode_tag or ""))
            if not path.is_file():
                raise FileNotFoundError(f"External subtitle file not found: {path}")
            sources.append(SubtitleSource(sub, None, path))
        else:
            idx = find_stream_index(
                src_file, "s", sub.language, sub.exclude_title_match, sub.match_title, sub.codec
            )
            if idx < 0:
                raise ValueError(
                    f"No subtitle matching language={sub.language!r} "
                    f"(title~={sub.match_title!r}) in {src_file}"
                )
            sources.append(SubtitleSource(sub, idx, None))
    return sources


def mux_episode(
    config: TitleConfig,
    src_file: Path,
    video_file: Path,
    audio_files: list[Path],
    subtitle_sources: list[SubtitleSource],
    out_file: Path,
) -> None:
    args = ["ffmpeg", "-y", "-i", str(video_file)]
    for audio_file in audio_files:
        args += ["-i", str(audio_file)]
    args += ["-i", str(src_file)]
    src_input_idx = len(audio_files) + 1

    external_input_idx: dict[int, int] = {}
    for i, sub_source in enumerate(subtitle_sources):
        if sub_source.external_path is not None:
            args += ["-i", str(sub_source.external_path)]
            external_input_idx[i] = src_input_idx + 1 + len(external_input_idx)

    args += ["-map", "0:v:0"]
    for t in range(len(audio_files)):
        args += ["-map", f"{t + 1}:a:0"]
    for i, sub_source in enumerate(subtitle_sources):
        if sub_source.stream_index is not None:
            args += ["-map", f"{src_input_idx}:s:{sub_source.stream_index}"]
        else:
            args += ["-map", f"{external_input_idx[i]}:s:0"]
    args += ["-map_chapters", str(src_input_idx)]

    if config.font_attach_dir:
        font_dir = Path(config.font_attach_dir)
        if font_dir.is_dir():
            font_files = sorted(p for p in font_dir.iterdir() if p.is_file())
            for i, font_file in enumerate(font_files):
                args += [
                    "-attach", str(font_file),
                    f"-metadata:s:t:{i}", "mimetype=application/x-truetype-font",
                ]

    args += ["-c:v", "copy", "-c:a", "copy", "-c:s", "copy"]

    # Release-group metadata (a container/stream title inherited from the source) otherwise
    # passes straight through even though the video itself was fully re-encoded, not copied -
    # override both explicitly with the computed clean title.
    title = clean_title(config, src_file)
    args += ["-metadata", f"title={title}", "-metadata:s:v:0", f"title={title}"]

    for t, track in enumerate(config.audio_tracks):
        args += [f"-disposition:a:{t}", "default" if track.default else "0"]
    for s, sub_source in enumerate(subtitle_sources):
        sub = sub_source.subtitle
        args += [
            f"-disposition:s:{s}", "default" if sub.default else "0",
            f"-metadata:s:s:{s}", f"title={sub.title}",
            f"-metadata:s:s:{s}", f"language={sub.language}",
        ]
        if sub.language_ietf:
            args += [f"-metadata:s:s:{s}", f"language-ietf={sub.language_ietf}"]

    args += [str(out_file)]
    subprocess.run(args, check=True, capture_output=True, text=True)
