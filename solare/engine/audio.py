"""Audio track transcoding - one pass per config.audio.tracks entry, always to Opus.

`mapping_family` and `channel_fix` are independent controls, both aimed at multichannel sources:
a track already in a channel layout ffmpeg understands natively only needs `mapping_family` (a
hint libopus needs to accept anything beyond stereo) with no remap at all; a track whose channel
order libopus can't make sense of as-is needs `channel_fix` (an explicit ffmpeg `-af` remap)
*together with* `mapping_family` - one alone isn't sufficient for that case. Multichannel
transcodes can fail silently (exit 0, valid-looking container, near-silent audio) without the
right mapping, which is why `transcode_audio_track` doesn't just trust its own exit code - see
`solare.engine.ffprobe.count_packets` used by the integrity check downstream.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from solare.engine.config import AudioTrack
from solare.engine.ffprobe import find_stream_index


def transcode_audio_track(
    src_file: Path, track: AudioTrack, source_language: str, out_file: Path
) -> None:
    src_index = find_stream_index(src_file, "a", source_language)
    if src_index < 0:
        raise ValueError(f"No audio stream matching language={source_language!r} in {src_file}")

    args = ["ffmpeg", "-y", "-i", str(src_file), "-map", f"0:a:{src_index}"]
    if track.kind == "downmix" and track.downmix_filter:
        args += ["-af", track.downmix_filter]
    elif track.channel_fix:
        args += ["-af", track.channel_fix]
    if track.mapping_family:
        args += ["-mapping_family", track.mapping_family]
    args += [
        "-c:a", "libopus", "-b:a", track.bitrate,
        "-metadata:s:a:0", f"title={track.title}",
        "-metadata:s:a:0", f"language={track.language}",
        str(out_file),
    ]
    subprocess.run(args, check=True, capture_output=True, text=True)
