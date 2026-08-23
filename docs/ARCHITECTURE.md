# Architecture

## Language & UI: Python + Textual
Cross-platform (Windows and Linux) without a compile step, with mature libraries for both
subprocess orchestration and terminal UI. `uv` manages all dependencies - never call
`python`/`pip` directly.

## Platform-specific code is isolated
OS-specific implementations (hardware sensor reading, process suspend/resume) live behind a
common interface in `solare/platform/`, with `windows/` and `linux/` implementations kept
physically separate rather than branched inline. The rest of the codebase depends only on the
common interface, never on which concrete implementation is active.

## Video encoding goes through av1an
Encoding is chunked and parallelized via [`av1an`](https://github.com/rust-av/Av1an) rather than
a single long-running encoder process. This gives genuine crash/power-loss resilience
(`av1an --resume` survives a hard kill, losing at most the one chunk that was in progress) and
better wall-clock throughput on multi-core hardware than one monolithic encode.

## Process suspend/resume is tree-aware
`av1an` spawns multiple parallel worker subprocesses, not just one. Pausing an encode suspends
the entire live process tree, re-walked on every check rather than enumerated once - a worker
that spawns mid-pause is caught on the very next check instead of running unpaused until the
pause ends.

## Repo structure
- `docs/` - committed, kept strictly to this tool's own architecture (this file). No personal
  file paths, no encoding-domain specifics, no development history.
- `history/` - gitignored; personal notes, per-title encoding investigation, and lessons learned
  along the way live here instead.
- `config/` - only `config.example.json` is committed; real per-title configs are gitignored.

## Publishing
Staged private-first, public later - the public flip is a deliberate, explicitly-confirmed step,
not inferred from "the code looks ready."
