"""av1an orchestration: invocation, done.json progress, process-tree-aware pause/resume.

Assumes `av1an` and its encoder dependencies (x265/SVT-AV1/etc, ffmpeg) are already resolvable on
PATH - see the README's Requirements section. No tool directory is hardcoded into the source -
point PATH at your own tools instead (or use `solare.engine.prepend_local_tools_to_path()`).

VapourSynth is the one dependency that can't be bundled in `tools/` - it needs a real, registered
install plus its own chunking plugins (`lsmas`/`ffms2`/`bs`/`vszip`/`julek` via `vsrepo`), verified
directly against a real encode end to end. See the README's Requirements section.

If `video.deinterlace`/`video.speedCorrection`/`video.upscale` is configured, av1an's own `-i`
points at a generated VapourSynth script (see engine/preprocess.py) instead of the raw source
file - av1an accepts a `.vpy` script as input directly, so chunking/encoding reads straight off
the filtered output with no separate full-file transcode pass.

Even when none of those is configured, `-i` still points at a generated (unfiltered) passthrough
.vpy rather than the raw source file directly, as long as the chunk method has a known VapourSynth
loader (see preprocess.supports_index_cache()) - purely so the source-plugin's own chunk-index
cache (e.g. lsmash's `<name>.lwi`) lands next to solare's own output instead of littering the
external source folder, which is where it goes by default with no cachedir override.

If `video.upscale` specifically is configured, av1an's own `--proxy` also points at a second,
cheaper generated script (same pipeline, upscale filter skipped) used only for scene-detection -
see preprocess.generate_proxy_vpy()'s own docstring for why: without it, scene-detection decodes
the entire source through the real (expensive) upscale filter just to look at frame differences,
then encoding runs the exact same upscale again per chunk for real.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import psutil

from solare import platform as solare_platform
from solare.engine.chunk_progress import ChunkProgress, find_latest_log, parse_chunk_progress
from solare.engine.config import TitleConfig
from solare.engine.preprocess import (
    generate_proxy_vpy,
    generate_vpy,
    needs_preprocessing,
    supports_index_cache,
)


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

        # Created eagerly (not deferred to start()) so build_args() reflects the real input path
        # even if called before start() - e.g. for logging what's about to run.
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._input_path = self._src_file
        self._proxy_path: Path | None = None
        if needs_preprocessing(config):
            # Deliberately NOT inside temp_dir: verified directly that av1an wipes/recreates its
            # own --temp directory on a fresh (non-resume) start, which silently deleted this file
            # out from under it - av1an would crash with no log output at all (a different flavor
            # of the same "don't put anything of your own inside av1an's --temp" lesson as
            # start()'s cwd handling below). video_out.parent is already where the log file goes,
            # so it's already established as solare-owned, av1an-observed-but-not-managed space.
            self._video_out.parent.mkdir(parents=True, exist_ok=True)
            vpy_path = self._video_out.parent / f"{self._video_out.stem}.preprocess.vpy"
            generate_vpy(config, self._src_file, vpy_path, self._chunk_method)
            self._input_path = vpy_path
            if config.video.upscale is not None:
                # A cheaper stand-in for av1an's own scene-detection pass specifically - see
                # generate_proxy_vpy()'s own docstring for why this matters (without it,
                # scene-detection runs the full TensorRT upscale on every frame, then throws the
                # result away, before encoding runs the exact same upscale again for real).
                proxy_path = self._video_out.parent / f"{self._video_out.stem}.proxy.vpy"
                generate_proxy_vpy(config, self._src_file, proxy_path, self._chunk_method)
                self._proxy_path = proxy_path
        elif supports_index_cache(self._chunk_method):
            # No real filtering needed for this title, but av1an's own chunking still builds a
            # source-plugin index (e.g. lsmash's <name>.lwi) - left to its default, that lands
            # right next to src_file, littering the external source folder rather than anything
            # solare owns. A minimal passthrough .vpy (just the loader line + cachedir, no actual
            # filters) gets the exact same cache-redirection generate_vpy() already does for a
            # real preprocessing script, without pretending this title needs one.
            self._video_out.parent.mkdir(parents=True, exist_ok=True)
            vpy_path = self._video_out.parent / f"{self._video_out.stem}.index.vpy"
            generate_vpy(config, self._src_file, vpy_path, self._chunk_method)
            self._input_path = vpy_path

    def build_args(self) -> list[str]:
        video = self._config.video
        video_params = f"--preset {video.preset} --crf {video.crf} {video.encoder_params}".strip()
        args = [
            "av1an",
            "-i", str(self._input_path),
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
        if self._proxy_path is not None:
            args += ["--proxy", str(self._proxy_path)]
        if video.crop and video.upscale is None:
            # When upscale is set, crop is already applied inside the generated .vpy script
            # (ahead of the upscale filter, which needs the cropped frame) - see preprocess.py.
            # Passing this flag too would crop twice.
            args += ["-f", f"-vf crop={video.crop}"]
        self._invalidate_resume_state_if_source_relocated()
        # done.json only exists once a real prior run has made progress - a better resumability
        # signal than bare directory existence, which __init__ now creates unconditionally (needed
        # early for preprocess.vpy generation, see __init__), so it would otherwise always be true.
        if (self._temp_dir / "done.json").exists():
            self._clean_orphaned_chunk_outputs()
            args += ["-r"]  # resume from an existing --temp dir rather than starting over
        return args

    def _invalidate_resume_state_if_source_relocated(self) -> None:
        """av1an's `chunks.json` freezes each chunk's own copy of the *original* run's script path
        (and full script_text) at scene-split time - confirmed directly by reading a real one - and
        `-r` reuses those per-chunk records verbatim, ignoring the fresh `-i` this run passes on the
        command line entirely. If the source/output ever moved (a drive renamed/consolidated, the
        whole project folder - source, --temp, done.json and all - copied to the new location
        together), every chunk's frozen record still points at a script path that no longer exists.

        Confirmed live on Ghost in the Shell: SAC_2045 (D:\\arc1 moved to H:\\, twice): first,
        every chunk failed with "x265: unable to open input file <->" (a *different*, now-fixed
        bug - see _clean_orphaned_chunk_outputs). Once that was fixed, the *real* failure surfaced
        on the next retry: "Script evaluation failed: File reading exception: [Errno 2] No such
        file or directory: 'D:\\arc1\\...\\....video.tmp.index.vpy'" - av1an trying to open a file
        on a drive letter that no longer had anything at that path, despite solare passing the
        correct new H:-based `-i`. A first attempt at fixing this compared the *.vpy file's* own
        content before/after regenerating it - reasonable in principle, but proven insufficient:
        it only catches a relocation at the exact moment it happens. By the time chunks.json is
        *already* corrupted from a *past* relocation (as here - the corruption happened on an
        earlier run, before this exact check existed), the .vpy snapshot comparison sees no change
        between "old" and "new" (both already say H:) and never fires, while chunks.json itself is
        still silently poisoned.

        The direct, unconditionally correct check instead: chunks.json (if present) must actually
        reference the input path this run is about to use - if it doesn't, by construction every
        chunk's frozen record points somewhere stale, regardless of *when* that happened. Wipe
        --temp's contents (not the directory itself, av1an recreates that) so the done.json check
        right after this finds nothing and starts genuinely fresh instead of resuming against
        state that can never work."""
        chunks_path = self._temp_dir / "chunks.json"
        if not chunks_path.exists():
            return
        try:
            chunks = json.loads(chunks_path.read_text())
            recorded_path = chunks[0]["input"]["VapourSynth"]["path"]
        except (json.JSONDecodeError, LookupError, TypeError):
            # An unreadable/unexpected shape is itself a reason not to trust this as a resumable
            # state - fail safe by treating it the same as a confirmed mismatch, below.
            recorded_path = None
        if recorded_path == str(self._input_path):
            return
        for child in self._temp_dir.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()

    def _clean_orphaned_chunk_outputs(self) -> None:
        """A chunk actively encoding at the moment of an unclean stop (Windows TerminateProcess,
        no chance for the encoder to finish or clean up - see terminate()'s own docstring) leaves
        a real, partial `encode/<chunk>.hevc` behind: done.json correctly never recorded it as
        finished, so a resume correctly re-queues it - but the stale partial file is still sitting
        at the exact path the redo is about to write to. Confirmed live on Ghost in the Shell:
        SAC_2045 (two chunks in flight when a run was stopped mid-encode): every resume attempt
        on those two chunks failed immediately with `x265: unable to open input file <->` and
        gave up after 3 retries, even though 146 other genuinely-finished chunks resumed and
        played back fine - only fixed by manually deleting the two stale partial files first.
        Whatever the exact mechanism (a leftover file at the encoder's expected output path
        interfering with a fresh open, most likely), the fix is unconditionally safe either way:
        any file in encode/ not listed in done.json's "done" map cannot be a real finished chunk
        by definition, so removing it before every resume costs nothing and never touches real
        progress. Not scoped to `.hevc` - other encoders in this project (SVT-AV1) write `.ivf`
        chunks instead, same failure class either way."""
        done_path = self._temp_dir / "done.json"
        encode_dir = self._temp_dir / "encode"
        if not encode_dir.is_dir():
            return
        done_chunks = set(json.loads(done_path.read_text()).get("done", {}))
        for chunk_file in encode_dir.iterdir():
            if chunk_file.is_file() and chunk_file.stem not in done_chunks:
                chunk_file.unlink()

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
        # (temp_dir itself is already created in __init__.)
        self._video_out.parent.mkdir(parents=True, exist_ok=True)
        self._process = subprocess.Popen(
            self.build_args(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(self._video_out.parent),
            creationflags=solare_platform.subprocess_creation_flags(),
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
            # Drop only pids that have genuinely exited - NOT merely ones that fell out of
            # current_pids. current_pids comes from root.children(recursive=True), which only
            # finds processes still reachable through a *live* parent chain; a chunk's actual
            # worker (x265/ffmpeg/vspipe) can keep running after an intermediate wrapper process
            # that spawned it has already exited, making it unreachable via that walk even though
            # it's still alive. Intersecting with current_pids there silently orphaned suspended
            # workers - confirmed live, they were never resumed and sat frozen indefinitely,
            # holding a chunk file open. psutil.pid_exists is the correct liveness check instead.
            self._suspended_pids = {pid for pid in self._suspended_pids if psutil.pid_exists(pid)}
        else:
            for pid in self._suspended_pids:
                solare_platform.resume_process(pid)
            self._suspended_pids.clear()

    def terminate(self) -> None:
        """Kills av1an's entire process tree, not just av1an itself. subprocess.terminate() on
        Windows is TerminateProcess() - an unconditional, immediate kill with no equivalent of
        POSIX SIGTERM, so av1an gets zero chance to clean up its own children before dying.
        Confirmed live: quitting the app left real x265/ffmpeg/vspipe chunk workers running,
        orphaned, indefinitely. Also kills anything still tracked in _suspended_pids - a pid can
        be a real, live, suspended process even when it's no longer reachable through
        _process_tree_pids()'s live parent-chain walk (see set_suspended's own comment on why),
        so relying on a fresh tree walk alone here would repeat that same gap."""
        if self._process is None:
            return
        pids = set(self._process_tree_pids()) | self._suspended_pids
        # Resume before killing - a stop can land while the tree is mid-suspend (solar gate or
        # manual pause), and an already-suspended process is worth waking first rather than
        # trusting kill() to tear it down cleanly from a frozen state on the first try.
        for pid in pids:
            solare_platform.resume_process(pid)
        for pid in pids:
            try:
                psutil.Process(pid).kill()
            except psutil.NoSuchProcess:
                pass
        self._suspended_pids.clear()
