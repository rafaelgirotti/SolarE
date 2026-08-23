"""Dolby Vision RPU injection - post-encode, not baked into the video encode pass itself.

Deliberately not using an encoder's own native DV RPU flags (avoids depending on a wrapper layer
correctly passing them through - real, open compatibility issues exist upstream for that path).
Instead: pull the encode down to a raw HEVC elementary stream, interleave the RPU NALs via
`dovi_tool`, then re-wrap into a proper .mkv via `mkvmerge` immediately.

The re-wrap step matters: ffmpeg's raw-HEVC demuxer doesn't generate valid per-packet timestamps
for a stream-copy into Matroska, even with an explicit framerate given before the input - it fails
outright ("Can't write packet with unknown timestamp"), and `-fflags +genpts` doesn't fix it.
`mkvmerge` infers the correct frame rate from a raw HEVC stream reliably, so wrapping through it
once, right after injection, means nothing downstream ever has to know this video passed through a
raw-stream stage at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def inject_rpu(video_in: Path, rpu_file: Path, video_out: Path, temp_dir: Path) -> None:
    """Extract `video_in`'s HEVC stream, inject the Dolby Vision RPU from `rpu_file`, and write
    the result as a proper .mkv to `video_out`. Raises subprocess.CalledProcessError on failure."""
    if not rpu_file.is_file():
        raise FileNotFoundError(f"Dolby Vision RPU file not found: {rpu_file}")

    temp_dir.mkdir(parents=True, exist_ok=True)
    raw_hevc = temp_dir / f"{video_in.stem}.hevc"
    injected_hevc = temp_dir / f"{video_in.stem}.injected.hevc"

    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(video_in),
                "-map", "0:v:0", "-c", "copy",
                "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", str(raw_hevc),
            ],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["dovi_tool", "inject-rpu", "-i", str(raw_hevc), "-r", str(rpu_file), "-o", str(injected_hevc)],
            check=True, capture_output=True, text=True,
        )
        # mkvmerge's exit codes: 0 success, 1 success-with-warnings (not a failure - the output
        # is still valid), 2 muxing aborted. check=True would misreport 1 as a hard failure.
        result = subprocess.run(
            ["mkvmerge", "-o", str(video_out), str(injected_hevc)],
            capture_output=True, text=True,
        )
        if result.returncode > 1:
            raise subprocess.CalledProcessError(
                result.returncode, result.args, result.stdout, result.stderr
            )
    finally:
        raw_hevc.unlink(missing_ok=True)
        injected_hevc.unlink(missing_ok=True)
