# SolarE

A video re-encoding orchestrator with solar-aware scheduling, crash-resumable chunked encoding,
real OS-level pause/resume, and live hardware monitoring.

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
- **HDR/Dolby-Vision aware.** Real Dolby Vision RPU passthrough, with x265/SVT-AV1 encoder
  parameters fully exposed and configurable per title rather than hardcoded.
- **Cross-platform.** Windows today, Linux support landing alongside it - platform-specific code
  (hardware sensors, process suspend/resume) is isolated behind a common interface rather than
  scattered through the codebase.

## Status

Early development. The project skeleton, solar polling, and hardware monitoring are in place;
the Textual dashboard and the `av1an` orchestration engine itself are still being built. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for design details and [Roadmap](#roadmap) below
for what's next.

## Requirements

- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) for dependency management - it also handles installing
  the right Python version for you, so a separate Python install isn't strictly required.
- External tools on your `PATH` (needed once the encoding engine is wired up):
  [`av1an`](https://github.com/rust-av/Av1an), a standalone `x265` and/or SVT-AV1 CLI build,
  `ffmpeg`/`ffprobe`, `mkvmerge` (from [MKVToolNix](https://mkvtoolnix.download/)), and
  [`dovi_tool`](https://github.com/quietvoid/dovi_tool) if you need Dolby Vision passthrough.
- Optional, only if you want solar-aware scheduling: a [Growatt](https://www.growatt.com/)
  inverter reachable via their cloud API (`--extra solar`).
- Optional, only for NVIDIA GPU stats in the hardware monitor: an NVIDIA GPU with drivers
  installed (`--extra gpu`). CPU/RAM monitoring works without it.

## Installation

```bash
git clone https://github.com/rafaelgirotti/SolarE.git
cd SolarE
uv sync
```

`uv sync` creates a `.venv` and installs every dependency pinned in `uv.lock` - no manual
`pip install` step, and no need to activate the virtualenv yourself; every command below runs
through `uv run` instead.

Verify the install:

```bash
uv run python -c "import solare; print('ok')"
```

To also pull in the optional solar-monitoring dependency:

```bash
uv sync --extra solar
```

## Configuration

Every encoding job is described by a JSON config. Copy the example and edit it for your source:

```bash
cp config/config.example.json config/my-title.json
```

See [`config/config.example.json`](config/config.example.json) for the full schema (source/output
paths, video codec and encoder params, audio track handling, subtitles). Real per-title configs
are gitignored - only `config.example.json` is tracked in the repo, so your own paths and
settings never end up in version control.

Solar-aware scheduling is optional and needs its own credentials file:

```bash
cp credentials.example.json credentials.json
# edit credentials.json with your Growatt account details
```

`credentials.json` is gitignored. See [`solare/solar/client.py`](solare/solar/client.py) for what
each field is used for, and confirm your inverter is `tlx`-family (via growattServer's
`device_list`) before relying on it - other Growatt device families expose different data.

## Development

```bash
solare/                  # application package
├── engine/               # queue building, config parsing, job orchestration
├── hwmonitor/             # CPU/GPU/RAM stats and temperatures
├── platform/              # OS-specific code behind a common interface
│   ├── windows/
│   └── linux/
├── solar/                 # inverter polling / solar-aware scheduling
└── tui/                   # the Textual dashboard
```

Run the full dependency + import sanity check with:

```bash
uv sync && uv run python -c "import solare; import solare.engine; import solare.hwmonitor; import solare.platform; import solare.solar; import solare.tui; print('ok')"
```

## Roadmap

- [x] Project skeleton, `uv`/dependency setup, architecture docs
- [x] Solar polling and hardware-monitoring modules
- [ ] Textual dashboard shell (mock data)
- [ ] Config schema + job queue engine
- [ ] `av1an` orchestration with process-tree-aware pause/resume
- [ ] Dolby Vision injection + audio/subtitle/mux pipeline
- [ ] Wire the dashboard to the real engine

## License

MIT - see [`LICENSE`](LICENSE).
