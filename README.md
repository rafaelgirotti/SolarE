# SolarE

A video re-encoding orchestrator with solar-aware scheduling, crash-resumable chunked encoding,
OS-level pause/resume, and live hardware monitoring.

## What makes this different

- **Solar-aware scheduling.** Gates the encode to actual production hours, polling a Growatt
  inverter directly - a title can auto-pause below a configured wattage and resume once
  generation picks back up, composing with manual pause rather than overriding it.
- **Crash-resumable, chunked encoding.** Video encoding is split into independently-encoded
  chunks (via [`av1an`](https://github.com/rust-av/Av1an)) - a hard kill, a power outage, or a
  deliberate pause loses at most one in-progress chunk, not the whole job.
- **OS-level pause/resume, not just a stop button.** A manual pause suspends the encode at the
  process level - zero work lost, resumes instantly regardless of how long the pause lasts.
- **Hardware monitoring alongside the job.** Live CPU/GPU/RAM stats and temperatures next to
  encode progress, not a bare progress bar.
- **HDR/Dolby-Vision aware.** Dolby Vision RPU passthrough, with x265/SVT-AV1 encoder parameters
  fully exposed and configurable per title rather than hardcoded.
- **Optional deinterlacing and speed correction.** QTGMC-based deinterlacing and linear frame-rate
  correction run as a VapourSynth preprocessing pass ahead of the encode itself - no separate
  transcode step.
- **Cross-platform.** Windows today, Linux support landing alongside it - platform-specific code
  (hardware sensors, process suspend/resume) is isolated behind a common interface rather than
  scattered through the codebase.

## Status

Load a title config in the dashboard, press Start, and it drives an `av1an` encode plus the full
Dolby Vision/audio/subtitle/mux pipeline in the background, with progress, ETA, and pause/resume
tracking the live process tree. Solar monitoring polls a Growatt inverter independently of any
running job, and a title config's `solarGate` connects that reading to the same pause path a
manual toggle uses. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for design details.

## Requirements

- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) for dependency management - it also handles installing
  the right Python version for you, so a separate Python install isn't strictly required.
- External encoder tools, resolvable on `PATH`:
  - [`av1an`](https://github.com/rust-av/Av1an) - `cargo install av1an`, or a prebuilt binary from
    its releases if you don't have a Rust toolchain.
  - `ffmpeg`/`ffprobe` - [official builds](https://ffmpeg.org/download.html).
  - A standalone `x265` CLI build - [MulticoreWare's builds](https://www.videolan.org/developers/x265.html)
    or build from source.
  - `SvtAv1EncApp` (SVT-AV1), if you want AV1 instead of HEVC - [releases](https://gitlab.com/AOMediaCodec/SVT-AV1/-/releases).
  - `mkvmerge` (from [MKVToolNix](https://mkvtoolnix.download/)).
  - [`dovi_tool`](https://github.com/quietvoid/dovi_tool), only if you need Dolby Vision passthrough.
  - [VapourSynth](https://www.vapoursynth.com/) plus its chunking plugins (L-SMASH/FFMS2/BestSource)
    - install via the official installer; a relocated/portable copy won't initialize, since its
    loader depends on OS-level registration, not just being on `PATH`. Follow
    [av1an's own installation instructions](https://github.com/rust-av/Av1an#installation) for the
    plugin setup (on Windows: `python3 vsrepo.py install lsmas ffms2 bs vszip julek` from
    VapourSynth's install directory) - this project doesn't duplicate that guide.
  - For deinterlacing (`video.deinterlace` in a title config): QTGMC's own dependency chain,
    installed into that same VapourSynth: `vsrepo install havsfunc mvsfunc mv rgvs nnedi3
    nnedi3_resample nnedi3_weights fmtc znedi3`, plus `pip install vsutil` (havsfunc's one
    pure-Python dependency, not a `vsrepo` package) into VapourSynth's own Python. Not needed
    unless a title actually uses `deinterlace`.

  **Convenience**: drop any of the above (except VapourSynth) into `tools/<name>/` next to this
  project (e.g. `tools/ffmpeg/ffmpeg.exe`) and `solare` prepends them to `PATH` automatically - no
  global install needed. `tools/` is gitignored; nothing in it is ever committed (these are large,
  platform-specific, often GPL-licensed binaries that don't belong in a git history).
- Optional, only if you want solar generation monitoring: a [Growatt](https://www.growatt.com/)
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

Solar monitoring is optional and needs its own credentials file:

```bash
cp credentials.example.json credentials.json
# edit credentials.json with your Growatt account details
```

`credentials.json` is gitignored. See [`solare/solar/client.py`](solare/solar/client.py) for what
each field is used for, and confirm your inverter is `tlx`-family (via growattServer's
`device_list`) before relying on it - other Growatt device families expose different data.

## Running the dashboard

```bash
uv run solare
```

Launches the Textual dashboard. CPU/GPU/RAM stats are read live from your machine; once a config
is loaded and started, encode-job progress (chunks, ETA, log lines) comes from the `av1an` process
actually encoding your source file. Make sure you're pointed at the right config before pressing
Start.

With no arguments, the dashboard starts idle - press `C` or click **Choose config** to pick a
`.json` file, then **Start**. `Ctrl+C` quits; `P` pauses/resumes, `T` stops.

Skip the picker and jump straight in:

```bash
uv run solare --config config/my-title.json --start
```

## Development

Project layout - `solare/` appears twice on purpose: the outer one is this repo (whatever you
named the folder you cloned into), the inner one is the actual Python package (`import solare`),
standard Python convention for naming the package directory after the project.

```bash
SolarE/                    # this repo (the folder name itself doesn't matter)
├── pyproject.toml
├── config/                 # config.example.json (real per-title configs are gitignored)
├── docs/                   # architecture, Growatt API reference
└── solare/                 # the Python package - everything below is `import solare....`
    ├── engine/               # queue building, config parsing, job orchestration
    ├── hwmonitor/             # CPU/GPU/RAM stats and temperatures
    ├── platform/              # OS-specific code behind a common interface
    │   ├── windows/
    │   └── linux/
    ├── solar/                 # inverter polling / solar generation monitoring
    └── tui/                   # the Textual dashboard
```

Run the full dependency + import sanity check with:

```bash
uv sync && uv run python -c "import solare; import solare.engine; import solare.hwmonitor; import solare.platform; import solare.solar; import solare.tui; print('ok')"
```

## License

MIT - see [`LICENSE`](LICENSE).
