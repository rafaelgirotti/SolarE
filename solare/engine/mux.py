"""Final mux: video + audio tracks + subtitles + chapters + font attachments, clean metadata.

Video and audio are always stream-copied here - both were already produced by earlier passes
(encode, and transcode-to-Opus respectively). Subtitles and chapters come from the source file by
default, from a standalone external file for a subtitle sourced outside it (e.g. a release with no
subtitle track at all in the language needed, filled in from a separate project's files), or
downloaded from OpenSubtitles (see engine/opensubtitles.py - the fetch happens in its own pipeline
phase before this module ever runs, this module only expects the result already on disk).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from solare.engine.config import Subtitle, TitleConfig
from solare.engine.ffprobe import count_streams, find_stream_index
from solare.engine.opensubtitles import download_path
from solare.engine.queue import clean_title


@dataclass
class SubtitleSource:
    subtitle: Subtitle
    stream_index: int | None  # set when subtitle.source == "primary"
    external_path: Path | None  # set when subtitle.source == "external"


def resolve_subtitle_sources(
    config: TitleConfig, src_file: Path, out_file: Path, episode_tag: str | None
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
        elif sub.source == "opensubtitles":
            # The subtitle-fetch phase (JobRunner._run_item, before this mux step) already
            # downloaded this - see opensubtitles.fetch_subtitle_for_item. Missing here means that
            # phase didn't run or failed silently, which shouldn't happen (it raises loudly on
            # failure) - this is a safety net, not the expected path to a missing file.
            path = download_path(out_file, sub)
            if not path.is_file():
                raise FileNotFoundError(
                    f"OpenSubtitles download for {sub.title!r} not found at {path} - the "
                    f"subtitle-fetch phase should have produced this before muxing"
                )
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
    # Carries over the source's own embedded attachments (fonts, almost always what these are on
    # a BD/anime remux) - needed for styled ASS subtitles to render with their intended fonts
    # instead of a fallback. "?" makes this an optional stream specifier - a no-op, not an error,
    # on a source with zero attachments. Verified directly: mapped correctly against a real
    # source with 12 font attachments. Separate from font_attach_dir below, which adds *new*
    # fonts from an external directory (e.g. for an externally-sourced subtitle track) - the two
    # are complementary, not overlapping.
    args += ["-map", f"{src_input_idx}:t?"]

    if config.font_attach_dir:
        font_dir = Path(config.font_attach_dir)
        if font_dir.is_dir():
            font_files = sorted(p for p in font_dir.iterdir() if p.is_file())
            # ffmpeg's -metadata:s:t:N addresses attachment-type OUTPUT streams by their overall
            # relative index, not specifically among the ones -attach just added - confirmed
            # live: with the source's own passed-through fonts absent from that count, the
            # metadata landed on a passed-through stream instead of the new one, leaving the
            # actually-new attachment with no mimetype and ffmpeg refusing to mux at all. Offset
            # by however many attachment streams the source already has ahead of these.
            attach_offset = count_streams(src_file, "t")
            for i, font_file in enumerate(font_files):
                t_index = attach_offset + i
                args += [
                    "-attach", str(font_file),
                    f"-metadata:s:t:{t_index}", "mimetype=application/x-truetype-font",
                ]

    args += ["-c:v", "copy", "-c:a", "copy", "-c:s", "copy", "-c:t", "copy"]

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


def add_subtitle_to_existing_output(
    existing_mkv: Path, new_subtitle_path: Path, subtitle: Subtitle, demote_language: str | None
) -> None:
    """Adds one new subtitle track to an ALREADY-FINISHED mux (a completed episode's output file),
    for backfilling a subtitle that wasn't available when it was first encoded - see
    history/backfill_monster_subtitles.py. Unlike mux_episode() (which builds a fresh mux from raw
    pieces produced this same run), this remuxes an existing file: every stream already in it
    passes through unchanged (`-map 0`, `-c copy`, disposition preserved automatically on copy)
    plus the new subtitle as one added stream. `demote_language` (e.g. "eng"), if given, is an
    existing subtitle language to flip off default for - only one subtitle should normally be
    flagged default, and `-c copy` alone doesn't change that on its own. Writes to a temp file and
    replaces the original only on success - a failed remux never damages a completed episode."""
    existing_sub_count = count_streams(existing_mkv, "s")
    demote_idx = find_stream_index(existing_mkv, "s", demote_language) if demote_language else -1

    tmp_out = existing_mkv.with_suffix(".subtitle-backfill.tmp" + existing_mkv.suffix)
    args = [
        "ffmpeg", "-y",
        "-i", str(existing_mkv),
        "-i", str(new_subtitle_path),
        "-map", "0", "-map", "1:0",
        "-map_chapters", "0",
        "-c", "copy",
    ]
    if demote_idx >= 0:
        args += [f"-disposition:s:{demote_idx}", "0"]
    new_sub_index = existing_sub_count
    args += [
        f"-disposition:s:{new_sub_index}", "default" if subtitle.default else "0",
        f"-metadata:s:s:{new_sub_index}", f"title={subtitle.title}",
        f"-metadata:s:s:{new_sub_index}", f"language={subtitle.language}",
    ]
    if subtitle.language_ietf:
        args += [f"-metadata:s:s:{new_sub_index}", f"language-ietf={subtitle.language_ietf}"]
    args += [str(tmp_out)]
    subprocess.run(args, check=True, capture_output=True, text=True)
    tmp_out.replace(existing_mkv)


def remove_subtitle_from_existing_output(
    existing_mkv: Path, stream_index: int, promote_language: str | None
) -> None:
    """Strips one existing subtitle stream (type-relative index, same convention as
    find_stream_index/SubtitleSource) from an ALREADY-FINISHED mux, e.g. to back out a
    previously-added track that turned out broken in playback - see
    history/strip_monster_pt_subtitles.py. `promote_language` (e.g. "eng"), if given, is an
    existing subtitle language to flip back to default - `-c copy` alone doesn't recompute
    disposition, so whichever track should become the new default needs it set explicitly here.
    Same temp-file-then-replace safety as add_/replace_subtitle_in_existing_output - a failed run
    never touches the original."""
    tmp_out = existing_mkv.with_suffix(".subtitle-remove.tmp" + existing_mkv.suffix)
    args = [
        "ffmpeg", "-y",
        "-i", str(existing_mkv),
        "-map", "0", "-map", f"-0:s:{stream_index}",
        "-map_chapters", "0",
        "-c", "copy",
    ]
    if promote_language:
        promote_idx = find_stream_index(existing_mkv, "s", promote_language)
        # The removed stream shifts every later subtitle's relative index down by one - only
        # adjust if the promoted track sat after the one being removed.
        if promote_idx > stream_index:
            promote_idx -= 1
        if promote_idx >= 0:
            args += [f"-disposition:s:{promote_idx}", "default"]
    args += [str(tmp_out)]
    subprocess.run(args, check=True, capture_output=True, text=True)
    tmp_out.replace(existing_mkv)


def replace_subtitle_in_existing_output(
    existing_mkv: Path, old_stream_index: int, new_subtitle_path: Path, subtitle: Subtitle
) -> None:
    """Swaps one existing subtitle stream (identified by its type-relative index, e.g. from
    find_stream_index - the same convention resolve_subtitle_sources/SubtitleSource already use)
    for a corrected replacement, without re-doing the rest of the mux. Added for a real case: a
    previously-added OpenSubtitles track had leftover ASS override codes baked into its text (see
    engine/opensubtitles.py's _ASS_OVERRIDE_RE comment) - re-running add_subtitle_to_existing_output
    with the cleaned file would have just added a *second* copy alongside the bad one, since that
    function has no notion of "already has this track, replace it." Every other stream (including
    every OTHER subtitle) passes through unchanged via `-map 0` plus one explicit exclusion of the
    old stream; the new file takes over that same disposition/metadata. Same temp-file-then-replace
    safety as add_subtitle_to_existing_output - a failed run never touches the original."""
    tmp_out = existing_mkv.with_suffix(".subtitle-fix.tmp" + existing_mkv.suffix)
    args = [
        "ffmpeg", "-y",
        "-i", str(existing_mkv),
        "-i", str(new_subtitle_path),
        "-map", "0", "-map", f"-0:s:{old_stream_index}",
        "-map", "1:0",
        "-map_chapters", "0",
        "-c", "copy",
    ]
    # The replacement lands as the new last subtitle-type output stream (everything before it
    # passes through in its original relative order, minus the excluded one) - same disposition/
    # metadata write as add_subtitle_to_existing_output's own new-track case.
    new_sub_index = count_streams(existing_mkv, "s") - 1
    args += [
        f"-disposition:s:{new_sub_index}", "default" if subtitle.default else "0",
        f"-metadata:s:s:{new_sub_index}", f"title={subtitle.title}",
        f"-metadata:s:s:{new_sub_index}", f"language={subtitle.language}",
    ]
    if subtitle.language_ietf:
        args += [f"-metadata:s:s:{new_sub_index}", f"language-ietf={subtitle.language_ietf}"]
    args += [str(tmp_out)]
    subprocess.run(args, check=True, capture_output=True, text=True)
    tmp_out.replace(existing_mkv)
