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
    its releases if you don't have a Rust toolchain. **Linux**: no prebuilt Linux binary exists in
    practice (checked - every recent release ships source only), so `cargo install av1an` is the
    real path, and it needs three things `cargo install` alone won't tell you up front: a `nasm`
    package (one of av1an's own dependencies needs it to assemble, fails with a clear "Unable to
    run nasm" otherwise), `--locked` on the install command itself (without it, `cargo install`
    re-resolves to a `vergen`/`vergen-lib` version combination that doesn't actually compile -
    unrelated to anything solare-specific, just how that dependency's version ranges happen to
    interact), and `vapoursynth-devel` (av1an links against VapourSynth's C libraries at build
    time unconditionally, even for a title that never touches a `.vpy` script).
  - `ffmpeg`/`ffprobe` - [official builds](https://ffmpeg.org/download.html).
  - A standalone `x265` CLI build - [MulticoreWare's builds](https://www.videolan.org/developers/x265.html)
    or build from source.
  - `SvtAv1EncApp` (SVT-AV1), if you want AV1 instead of HEVC - [releases](https://gitlab.com/AOMediaCodec/SVT-AV1/-/releases).
  - `mkvmerge` (from [MKVToolNix](https://mkvtoolnix.download/)).
  - [`dovi_tool`](https://github.com/quietvoid/dovi_tool), only if you need Dolby Vision passthrough.
  - [VapourSynth](https://www.vapoursynth.com/) plus its chunking plugins (L-SMASH/FFMS2/BestSource)
    - a relocated/portable copy won't initialize, since its loader depends on OS-level
    registration, not just being on `PATH`. Follow
    [av1an's own installation instructions](https://github.com/rust-av/Av1an#installation) for the
    general approach - this project doesn't duplicate that guide - but read the two Linux notes
    below before running `vsrepo`, since the official guide's `vsrepo` instructions alone produce
    a broken install there.
    - **Windows**: install via the official installer, then `python3 vsrepo.py install lsmas ffms2
      bs vszip julek` from VapourSynth's install directory.
    - **Linux**: install VapourSynth itself from your distro (Fedora/RPM Fusion:
      `vapoursynth-libs vapoursynth-devel vapoursynth-tools python3-vapoursynth`; confirmed working
      this way, no registry-equivalent step needed - Linux's dynamic linker finds it through the
      normal system search path). **Don't `pip install vsrepo`** for the chunking plugins despite
      what av1an's own guide suggests - confirmed live: it pulls in its own bundled VapourSynth
      core (a different version from your distro's) as a dependency, which silently shadows the
      system one for every process on the machine, not just `vsrepo` itself - broke `vspipe` and
      `av1an`'s own VapourSynth detection outright (`Failed to get VSScript API`) until traced back
      and removed. If you've already hit that: `pip uninstall vapoursynth vsrepo vsstubs` fixes it.
      Instead, either build each plugin from source against your distro's VapourSynth
      (`vapoursynth-devel`), or use `pip install --user vsrepo` **only** to fetch the plugin
      binaries it has as prebuilt Linux downloads (`ffms2`, `bestsource` - confirmed available;
      `lsmas`/`vszip` are not, as of this writing), then immediately `pip uninstall` it again and
      manually copy the downloaded `.so` file(s) from
      `~/.local/lib/python3.*/site-packages/vapoursynth/plugins/vsrepo/` into your distro
      VapourSynth's own plugin autoload directory (Fedora: `/usr/lib64/vapoursynth/`, confirmed via
      `ldconfig -p`/`ldd` on the system `vspipe` binary - don't assume the path, check it) - that's
      the directory av1an (linked against the *system* VapourSynth) actually scans, not wherever
      the pip package's own bundled copy would put things.
    - **Linux, building `lsmash` from source** (no prebuilt Linux binary exists anywhere, confirmed
      - `vsrepo`'s own index only lists Windows binaries for it): clone
      [`HomeOfAviSynthPlusEvolution/L-SMASH-Works`](https://github.com/HomeOfAviSynthPlusEvolution/L-SMASH-Works)
      with `--recurse-submodules`, then:
      ```
      cmake -B build -S . -DBUILD_AVS_PLUGIN=OFF -DBUILD_AU2_PLUGIN=OFF -DENABLE_MFX=OFF \
        -Ddav1d_USE_STATIC_LIBS=OFF -DVPX_USE_STATIC_LIBS=OFF -DZLIB_USE_STATIC_LIBS=OFF \
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DCMAKE_BUILD_TYPE=Release
      cmake --build build -j$(nproc)
      ```
      (Fedora build deps: `cmake ffmpeg-devel zlib-devel libdav1d-devel libvpx-devel
      libxml2-devel`.) `-DCMAKE_POSITION_INDEPENDENT_CODE=ON` is required, not optional - without
      it the final link fails (`R_X86_64_32S relocation ... recompile with -fPIC`), since the
      bundled `xxHash`/`l-smash`/`obuparse` static libs it links against aren't built with PIC by
      default and this is the one flag that fixes all of them at once. Copy the resulting
      `build/libLSMASHSource.so` into the same plugin autoload directory as above. Confirmed
      working end to end afterward: a real `av1an` chunked encode via `-m lsmash` through solare's
      own dashboard, not just av1an's own `--version` plugin self-check (which only proves the
      plugin *loads*, not that it actually indexes/decodes a real file correctly).
  - For deinterlacing (`video.deinterlace` in a title config): QTGMC's own dependency chain,
    installed into that same VapourSynth: `vsrepo install havsfunc mvsfunc mv rgvs nnedi3
    nnedi3_resample nnedi3_weights fmtc znedi3`, plus `pip install vsutil` (havsfunc's one
    pure-Python dependency, not a `vsrepo` package) into VapourSynth's own Python. Not needed
    unless a title actually uses `deinterlace`.
  - For AI upscaling (`video.upscale` in a title config): [vs-mlrt](https://github.com/AmusementClub/vs-mlrt)'s
    `vstrt`/`vsncnn` plugin files placed into that same VapourSynth's own plugins directory (same
    OS-level-registration reason as VapourSynth itself above - not something `tools/` PATH-prepend
    can do), a matching-version TensorRT runtime, and a TensorRT engine pre-built for the exact
    title's post-crop resolution. See [`../tools/vsmlrt.md`](../tools/vsmlrt.md) and
    [`../tools/cuda-tensorrt.md`](../tools/cuda-tensorrt.md) for the full Windows setup and the
    real gotchas found doing it the first time - genuinely more involved than the other tools
    here, not a quick `vsrepo install`. Not needed unless a title actually uses `upscale`.
    - **Linux**: no prebuilt binary exists for `vstrt` on any platform but Windows - confirmed
      against every upstream vs-mlrt release asset - so it has to be built from source, and three
      more gaps show up doing that, none of them documented upstream:
      1. **TensorRT's own `pip install tensorrt` ships the runtime `.so` files but no C++
         headers and no `trtexec` binary** - fine for Python-side inference, not enough to
         compile `vstrt` or build an engine the traditional way. Get the matching-version headers
         from NVIDIA's own open-source [`NVIDIA/TensorRT`](https://github.com/NVIDIA/TensorRT)
         repo instead (checkout the tag matching the pip version, e.g. `v11.3` for
         `tensorrt==11.3.0.99`) - `include/` alone is enough, no account/login gate, same
         no-SDK-download principle as the pip route itself.
      2. **The built `libvstrt.so` can't find `libnvinfer.so.11` at runtime** - pip installs it
         into a user site-packages directory, nowhere the dynamic linker searches by default.
         Fix: copy the actual `.so` file (not a symlink into site-packages - it needs to survive
         a `pip uninstall`) into VapourSynth's plugin autoload directory alongside `vstrt.so`
         itself, and register that same directory as a real library path (a one-line
         `/etc/ld.so.conf.d/*.conf` file + `ldconfig`) - it's already serving double duty as a
         plugin dir, this just makes it a library dir too.
      3. **`trt.BuilderFlag.FP16` no longer exists as of TensorRT 11.3** (present in the version
         `vsmlrt.md`'s Windows research was done against) - precision handling changed in
         TensorRT 10+ and the flag was removed outright, not renamed to anything obvious. Building
         an engine via TensorRT's Python API (`trt.Builder`/`trt.OnnxParser`, since there's no
         `trtexec` binary either - see gap 1) without it still produces a working fp32 engine;
         getting real fp16 throughput back needs whatever TensorRT 11's actual replacement
         mechanism is - not yet chased down, worth revisiting before trusting throughput numbers
         from the old Windows benchmarks.

      Build (Fedora, RTX 50-series example - adjust the CUDA/TensorRT package versions to match
      your own driver):
      ```
      sudo dnf install cuda-cudart-13-4 cuda-cudart-devel-13-4 cuda-driver-devel-13-4 cuda-nvcc-13-4
      python3.14 -m pip install --user tensorrt   # runtime .so files + Python API
      git clone --branch v11.3 --depth 1 https://github.com/NVIDIA/TensorRT.git trt-oss  # headers only

      git clone --depth 1 --branch v15.16 https://github.com/AmusementClub/vs-mlrt.git
      cd vs-mlrt/vstrt
      cmake -B build -S . \
        -DVAPOURSYNTH_INCLUDE_DIRECTORY=/usr/include/vapoursynth \
        -DTENSORRT_HOME=<a dir with include/ symlinked to trt-oss/include, lib/ symlinked to
                          the unversioned .so names pointing at ~/.local/.../tensorrt_libs/*.so.11>
      cmake --build build -j$(nproc)
      ```
      Then build the engine itself with a short Python script calling the `tensorrt` API directly
      (`Builder` → `OnnxParser.parse(onnx_bytes)` → `create_optimization_profile()` with
      `min=opt=max=(1,3,height,width)` matching the config's exact post-crop resolution, matching
      the shape-must-be-exact gotcha `vsmlrt.md` already documents → `build_serialized_network()`),
      saved to the exact path `engine_path()` in `solare/engine/preprocess.py` expects
      (`<shared tools>/vsmlrt/vstrt/<model>_<width>x<height>.engine`) - solare's own error message
      when an engine is missing already prints the equivalent `trtexec` invocation as a reference.
      Confirmed working end to end: a real 1080p→4K encode through solare's own dashboard, not
      just an engine-load self-check.

  **Convenience**: drop any of the above (except VapourSynth) into `<name>/` under a shared
  `tools/` directory one level up from this project (i.e. a sibling of every project that wants
  the same tools - `../tools/ffmpeg/ffmpeg.exe`, `../tools/av1an/av1an.exe`, etc.) and `solare`
  prepends them to `PATH` automatically - no global install needed, and no separate copy per
  project either. See that directory's own README for the convention. A project-local
  `tools/<name>/` (directly next to this project, not the shared one) still works too, and takes
  priority over the shared copy - useful for pinning a different build/version just for solare.
  Either location is gitignored; nothing in either is ever committed (these are large,
  platform-specific, often GPL-licensed binaries that don't belong in a git history).
- Optional, only if you want solar generation monitoring: a [Growatt](https://www.growatt.com/)
  inverter reachable via their cloud API, plus `credentials.json` (see Configuration below) - the
  `growattServer` package itself is always installed, it just goes unused without credentials.
- Optional, only for NVIDIA GPU stats in the hardware monitor: an NVIDIA GPU with drivers
  installed. CPU/RAM monitoring works without it.
- Optional, only for real CPU *power* readings in the hardware monitor on Linux: a udev rule
  making the RAPL package zone's `energy_uj` readable without root - a stock kernel (confirmed on
  Fedora) ships it root-only by default (RAPL side-channel hardening, CVE-2020-8694-adjacent).
  Without it the panel just shows CPU power as `n/a`; CPU temperature, GPU stats, and everything
  else in the hardware monitor work regardless. To enable it:
  ```
  # /etc/udev/rules.d/99-rapl-permissions.rules
  SUBSYSTEM=="powercap", KERNEL=="intel-rapl:*", RUN+="/usr/bin/chmod -R a+r /sys%p"
  ```
  then `sudo udevadm control --reload-rules && sudo udevadm trigger --action=add
  --subsystem-match=powercap && sudo udevadm settle` to apply immediately (or just reboot - it
  reapplies automatically on every boot from here on). Not part of this repo or its `tools/`
  convention since it's a system-level permission change, not a file solare ships or reads from a
  project directory - if you ever rebuild your initramfs or move to a different machine, this
  rule needs to be recreated there too.
- Optional, only if a title's config uses `source: "opensubtitles"` for a subtitle track: an
  OpenSubtitles.com account (API key + login) - see `config/config.example.json`'s
  `openSubtitles` block.

## Installation

```bash
git clone https://github.com/rafaelgirotti/SolarE.git
cd SolarE
uv sync
```

`uv sync` creates a `.venv` and installs every dependency pinned in `uv.lock` - no manual
`pip install` step, and no need to activate the virtualenv yourself; every command below runs
through `uv run` instead. Solar monitoring, GPU stats, and OpenSubtitles support are all base
dependencies (not optional extras) - each degrades gracefully at runtime if genuinely unused
(no credentials, no GPU, no title configured to use it), so there's nothing extra to opt into.

Verify the install:

```bash
uv run python -c "import solare; print('ok')"
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
