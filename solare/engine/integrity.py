"""Post-mux integrity check: catches a muxed file that's technically valid but actually broken.

Duration/stream-count checks catch a bad map. The per-track packet-count check catches something
those can't: a multichannel-to-Opus transcode that exits 0, produces a valid-looking container,
and plays back as near-silent - only a handful of audio packets instead of tens of thousands, from
a channel-mapping mismatch libopus didn't error out on. Exit code and file existence alone would
call that success.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from solare.engine.ffprobe import count_packets, count_streams, get_duration

_MIN_OUTPUT_SIZE_BYTES = 1024 * 1024
_MAX_DURATION_DRIFT_SECONDS = 2.0
_MIN_AUDIO_PACKETS = 500


@dataclass
class IntegrityResult:
    ok: bool
    reason: str


def check_output_integrity(
    source_file: Path, output_file: Path, expected_audio_count: int, expected_sub_count: int
) -> IntegrityResult:
    if not output_file.is_file():
        return IntegrityResult(False, f"{output_file} does not exist")

    size = output_file.stat().st_size
    if size < _MIN_OUTPUT_SIZE_BYTES:
        return IntegrityResult(False, f"{output_file} is suspiciously small ({size} bytes)")

    src_duration = get_duration(source_file)
    out_duration = get_duration(output_file)
    if out_duration == 0 or abs(src_duration - out_duration) > _MAX_DURATION_DRIFT_SECONDS:
        return IntegrityResult(
            False, f"duration mismatch - source {src_duration}s vs output {out_duration}s"
        )

    video_count = count_streams(output_file, "v")
    audio_count = count_streams(output_file, "a")
    sub_count = count_streams(output_file, "s")
    if video_count != 1 or audio_count != expected_audio_count or sub_count != expected_sub_count:
        return IntegrityResult(
            False,
            f"unexpected stream counts - video={video_count} audio={audio_count} sub={sub_count} "
            f"(expected 1, {expected_audio_count}, {expected_sub_count})",
        )

    for a in range(expected_audio_count):
        packets = count_packets(output_file, f"a:{a}")
        if packets < _MIN_AUDIO_PACKETS:
            return IntegrityResult(
                False, f"audio track a:{a} has suspiciously few packets ({packets})"
            )

    return IntegrityResult(
        True,
        f"duration {out_duration}s (source {src_duration}s), streams v={video_count} "
        f"a={audio_count} s={sub_count}",
    )
