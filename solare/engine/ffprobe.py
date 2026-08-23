"""ffprobe-backed stream inspection: locating source tracks, and post-mux integrity checks."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def find_stream_index(
    file: Path,
    stream_type: str,
    language: str,
    exclude_title_match: str | None = None,
    match_title: str | None = None,
    codec: str | None = None,
) -> int:
    """Return the 0-based index (relative to streams of this type only, matching ffmpeg's own
    `0:a:N`/`0:s:N` stream-selector numbering) of the first stream matching `language` plus the
    optional title/codec filters. Returns -1 if nothing matches.

    `codec` filters by codec_name (e.g. "ass" vs "hdmv_pgs_subtitle") for sources where title text
    alone isn't a reliable enough signal (inconsistent or missing title tags across episodes of
    the same release, but a consistent codec per track role).
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", stream_type,
            "-show_entries", "stream=index,codec_name:stream_tags=language,title",
            "-of", "json", "--", str(file),
        ],
        capture_output=True, text=True, check=True,
    )
    streams = json.loads(result.stdout).get("streams", [])

    rel_index = 0
    for stream in streams:
        tags = stream.get("tags", {})
        title = tags.get("title")
        title_excluded = bool(
            exclude_title_match and title and re.search(exclude_title_match, title)
        )
        title_matches = not match_title or (title and re.search(match_title, title))
        codec_matches = not codec or stream.get("codec_name") == codec
        if (
            tags.get("language") == language
            and not title_excluded
            and title_matches
            and codec_matches
        ):
            return rel_index
        rel_index += 1
    return -1


def get_duration(file: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", "--", str(file)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def count_streams(file: Path, stream_type: str) -> int:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream_type, "-show_entries", "stream=index", "-of", "csv=p=0", "--", str(file)],
        capture_output=True, text=True, check=True,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return len(lines)


def count_packets(file: Path, stream_selector: str) -> int:
    """`stream_selector` is an ffprobe stream-selector string, e.g. "a:0"."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", stream_selector,
            "-count_packets", "-show_entries", "stream=nb_read_packets",
            "-of", "csv=p=0", "--", str(file),
        ],
        capture_output=True, text=True, check=True,
    )
    output = result.stdout.strip()
    return int(output) if output else 0
