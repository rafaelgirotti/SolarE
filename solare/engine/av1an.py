"""av1an orchestration: invocation, done.json progress, process-tree-aware pause/resume.

Assumes `av1an` and its encoder dependencies (x265/SVT-AV1/etc, ffmpeg) are already resolvable on
PATH - see the README's Requirements section. No tool directory is hardcoded into the source -
point PATH at your own tools instead.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import psutil

from solare import platform as solare_platform
from solare.engine.config import TitleConfig


@dataclass
class Av1anProgress:
    done_frames: int
    total_frames: int

    @property
    def fraction(self) -> float:
        return self.done_frames / self.total_frames if self.total_frames else 0.0


class Av1anRunner:
    """One av1an invocation - one source file encoded to one video-only output. Audio, subtitles,
    and muxing are separate passes outside this class."""

    def __init__(self, config: TitleConfig, src_file: Path, video_out: Path, temp_dir: Path):
        self._config = config
        self._src_file = src_file
        self._video_out = video_out
        self._temp_dir = temp_dir
        self._process: subprocess.Popen | None = None
        self._suspended_pids: set[int] = set()

    def build_args(self) -> list[str]:
        video = self._config.video
        video_params = f"--preset {video.preset} --crf {video.crf} {video.encoder_params}".strip()
        args = [
            "av1an",
            "-i", str(self._src_file),
            "-o", str(self._video_out),
            "-e", video.codec,
            "-v", video_params,
            "-a", "-an",
            "--temp", str(self._temp_dir),
            "-k", "-y",
        ]
        if video.pix_fmt:
            args += ["--pix-format", video.pix_fmt]
        return args

    def start(self) -> None:
        self._process = subprocess.Popen(
            self.build_args(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def poll(self) -> int | None:
        """Return the exit code once av1an has finished, else None while still running."""
        if self._process is None:
            return None
        return self._process.poll()

    def get_progress(self) -> Av1anProgress | None:
        """Read av1an's own <temp>/done.json - authoritative frames-done/frames-total, updated as
        each chunk finishes. Returns None if the file doesn't exist yet or is mid-write (av1an can
        be writing it at the exact moment this is called) - tolerate that as "not ready yet," not
        an error.
        """
        done_file = self._temp_dir / "done.json"
        if not done_file.exists():
            return None
        try:
            data = json.loads(done_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        total_frames = data.get("frames")
        if not total_frames:
            return None
        done_frames = sum(chunk["frames"] for chunk in data.get("done", {}).values())
        return Av1anProgress(done_frames=done_frames, total_frames=total_frames)

    def _process_tree_pids(self) -> list[int]:
        if self._process is None:
            return []
        try:
            root = psutil.Process(self._process.pid)
        except psutil.NoSuchProcess:
            return []
        return [self._process.pid] + [p.pid for p in root.children(recursive=True)]

    def set_suspended(self, should_be_suspended: bool) -> None:
        """Call every tick, not just on the transition edge - av1an spawns worker subprocesses
        dynamically as chunks start/finish, so re-walking the tree fresh each call (rather than
        enumerating it once) catches a worker that spawns mid-pause on the very next call instead
        of letting it run unpaused until the pause ends.
        """
        current_pids = set(self._process_tree_pids())
        if should_be_suspended:
            for pid in current_pids - self._suspended_pids:
                if solare_platform.suspend_process(pid):
                    self._suspended_pids.add(pid)
            self._suspended_pids &= current_pids
        else:
            for pid in self._suspended_pids:
                solare_platform.resume_process(pid)
            self._suspended_pids.clear()

    def terminate(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
