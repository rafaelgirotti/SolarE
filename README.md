# SolarE

A video re-encoding orchestrator built for people who care about grain-preserving archival
quality, run heavy encodes on solar power, and don't want to lose days of progress to a power
outage.

## What makes this different

- **Solar-aware scheduling.** Gates CPU-heavy encoding to actual panel-production hours instead
  of a fixed clock window, polling real inverter data rather than guessing.
- **Crash-resumable, chunked encoding.** Video encoding is split into independently-encoded
  chunks (via [`av1an`](https://github.com/rust-av/Av1an)) - a hard kill, a power outage, or a
  deliberate pause loses at most one in-progress chunk, not the whole job.
- **Real pause/resume, not just a stop button.** A deliberate pause (solar dipping, or a manual
  toggle) suspends the encode at the OS process level - zero work lost, resumes instantly,
  regardless of how long the pause lasts.
- **Actually monitors your hardware.** Live CPU/GPU/RAM stats and temperatures alongside encode
  progress, not a bare progress bar.
- **Built for grain-critical, HDR/Dolby-Vision content.** Tuned x265/SVT-AV1 recipes for
  film-grain-heavy sources, with real Dolby Vision RPU passthrough support.

## Status

Early development - not yet usable end-to-end. See `docs/ARCHITECTURE.md` for design details.

## Requirements

- [`uv`](https://github.com/astral-sh/uv) for Python dependency management - no separate install
  step, `uv run` handles the environment.
- External tools on `PATH`: [`av1an`](https://github.com/rust-av/Av1an), a standalone `x265`
  and/or SVT-AV1 CLI build, `ffmpeg`/`ffprobe`, `mkvmerge` (from MKVToolNix), and
  [`dovi_tool`](https://github.com/quietvoid/dovi_tool) if you need Dolby Vision passthrough.

## Setup

```bash
uv sync
cp config/config.example.json config/my-title.json
# edit config/my-title.json with your real source/output paths and recipe
```

Real per-title configs are gitignored - `config/config.example.json` is the only one tracked in
the repo. See that file for the full schema.

## License

MIT - see `LICENSE`.
