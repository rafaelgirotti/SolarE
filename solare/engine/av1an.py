"""av1an orchestration: invocation, done.json progress, process-tree-aware pause/resume.

Assumes `av1an` and its encoder dependencies (x265/SVT-AV1/etc, ffmpeg) are already resolvable on
PATH - see the README's Requirements section. No tool directory is hardcoded into the source -
point PATH at your own tools instead (or use `solare.engine.prepend_local_tools_to_path()`).

VapourSynth is the one dependency that can't be bundled in `tools/` - it needs a real, registered
install plus its own chunking plugins (`lsmas`/`ffms2`/`bs`/`vszip`/`julek` via `vsrepo`), verified
directly against a real encode end to end. See the README's Requirements section.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import psutil

from solare import platform as solare_platform
from solare.engine.chunk_progress import ChunkProgress, find_latest_log, parse_chunk_progress
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

    def __init__(
        self,
        config: TitleConfig,
        src_file: Path,
        video_out: Path,
        temp_dir: Path,
        chunk_method: str = "lsmash",
    ):
        self._config = config
        # Resolved to absolute up front - av1an is launched with its cwd changed (see start()),
        # so any relative path here would otherwise resolve against the wrong directory.
        self._src_file = src_file.resolve()
        self._video_out = video_out.resolve()
        self._temp_dir = temp_dir.resolve()
        # A resumed run must use the same chunk method its existing --temp progress was built
        # with - av1an's own split/loadscript.vpy already hardcodes it from the original run, and
        # a mismatched -m flag on resume is a real inconsistency risk. lsmash is the default for a
        # fresh run (see build_args()'s comment on why); override for anything resuming from a
        # --temp directory that used a different method.
        self._chunk_method = chunk_method
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
            # Explicit chunk-method rather than av1an's own default. Verified directly: av1an's
            # ffmpeg-based fallback chunk methods (its likely default, and "hybrid"/"segment"
            # explicitly) still pass ffmpeg's removed -vsync flag and crash with "Unrecognized
            # option 'vsync'" on current ffmpeg builds. Any VapourSynth-plugin-based method
            # (lsmash, the default here; bestsource, ffms2, ...) sidesteps that entirely.
            "-m", self._chunk_method,
            "--temp", str(self._temp_dir),
            "-k", "-y",
        ]
        if video.pix_fmt:
            args += ["--pix-format", video.pix_fmt]
        if video.crop:
            args += ["-f", f"-vf crop={video.crop}"]
        # done.json only exists once a real prior run has made progress - a better resumability
        # signal than bare directory existence, which start() now creates unconditionally before
        # launching (needed as av1an's own cwd), so it would otherwise always be true.
        if (self._temp_dir / "done.json").exists():
            args += ["-r"]  # resume from an existing --temp dir rather than starting over
        return args

    def start(self) -> None:
        # av1an's own log defaults to ./logs/av1an.log.<date>, relative to wherever the process
        # was launched from - explicitly passing --log-file to redirect it was tried and
        # confirmed unreliable on this av1an build (silently produced no file at all, even with
        # the target directory pre-created). Controlling cwd instead is more robust: av1an's
        # *default* log path then resolves inside the video output's own directory, fully
        # contained the same way every other av1an-generated file already is.
        #
        # Deliberately the output directory, not temp_dir itself: cwd == the exact --temp path
        # av1an is about to create/manage made it fail outright (STATUS_DLL_NOT_FOUND-style
        # generic crash, no log, nothing written) - likely a conflict between av1an trying to
        # (re)create that directory and it also being the process's own working directory.
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._video_out.parent.mkdir(parents=True, exist_ok=True)
        self._process = subprocess.Popen(
            self.build_args(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(self._video_out.parent),
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

    def get_chunk_progress(self) -> ChunkProgress | None:
        """Per-worker in-progress chunk timing, parsed from av1an's own log (see
        engine.chunk_progress) - the log lives at <cwd>/logs/av1an.log.<date>, cwd being
        video_out.parent (see start()'s own comment on why). Returns None before the log exists
        yet (encode not started) or if nothing's parseable from it."""
        logs_dir = self._video_out.parent / "logs"
        if not logs_dir.is_dir():
            return None
        log_path = find_latest_log(logs_dir)
        if log_path is None:
            return None
        return parse_chunk_progress(log_path)

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
