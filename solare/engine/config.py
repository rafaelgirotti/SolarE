"""Per-title job configuration - parses the schema in config/config.example.json.

No validation framework: a missing required field raises a plain KeyError with the field name in
it, which is enough for a config file the user wrote by hand and can fix immediately - this isn't
attacker-controlled input that needs defensive parsing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DeinterlaceSettings:
    """QTGMC (havsfunc) deinterlacing, applied as a VapourSynth preprocessing pass ahead of
    av1an's own chunking/encoding - see engine/preprocess.py. `params` passes straight through as
    QTGMC keyword arguments (e.g. {"Preset": "Slower", "MatchPreset": "Slower"}), since QTGMC has
    far more tunable parameters than are worth enumerating as typed fields - same passthrough
    philosophy as VideoSettings.encoder_params."""

    tff: bool = True  # field order: top-field-first (most common) vs bottom-field-first
    fps_divisor: int = 2  # QTGMC's own default (1) bobs to double-rate output (one frame per
    # field) - verified directly against a real clip (50 interlaced frames in, 100 out at
    # FPSDivisor=1). 2 gives single-rate output (one frame per original interlaced frame,
    # matching input frame count) - the right choice unless the source has genuine per-field
    # motion worth preserving as its own frames, which most re-interlaced-for-broadcast content
    # doesn't have.
    params: dict[str, str | int | float | bool] = field(default_factory=dict)


@dataclass
class SpeedCorrection:
    """Corrects a linear speed shift (e.g. a PAL-television master of a source that was mastered
    at a different frame rate) - both video and audio need this together, or their sync drifts.
    Only a 1:1 frame-timing relabel plus an audio pitch/tempo correction, not resampling: this
    assumes every source frame already exists and is just mistimed, not that frames were
    added/dropped (that's a different problem this doesn't address)."""

    source_fps: str  # e.g. "25" - the rate the source is currently timed at
    target_fps: str  # e.g. "24000/1001" - the rate it should actually play at

    @property
    def ratio(self) -> float:
        """target/source - multiply a duration or sample rate by this to correct it. Below 1.0
        means the source plays too fast and needs slowing down (and its audio pitch dropped to
        match); above 1.0 is the reverse."""
        return _parse_fps_fraction(self.target_fps) / _parse_fps_fraction(self.source_fps)


def _parse_fps_fraction(fps: str) -> float:
    if "/" in fps:
        num, den = fps.split("/", 1)
        return float(num) / float(den)
    return float(fps)


@dataclass
class VideoSettings:
    codec: str
    preset: str
    crf: str
    pix_fmt: str
    encoder_params: str
    crop: str | None = None
    dovi_rpu: str | None = None
    chunk_method: str = "lsmash"  # av1an -m; override to match an already-existing --temp dir's
    # own chunk method when resuming a run that used a different one (see temp_dir below)
    temp_dir: str | None = None  # override av1an's --temp path (default: computed next to the
    # output file); needed to resume progress sitting in a folder outside that naming convention
    deinterlace: DeinterlaceSettings | None = None
    speed_correction: SpeedCorrection | None = None


@dataclass
class SourceFolder:
    src: str
    out: str  # "" flattens straight into output.root instead of mirroring a subfolder


@dataclass
class NamingTransform:
    pattern: str
    replacement: str


@dataclass
class AudioTrack:
    kind: str  # "native" (transcode the matching source track) or "downmix"
    bitrate: str
    title: str
    language: str
    default: bool = False
    mapping_family: str | None = None  # libopus channel-layout hint for >2-channel tracks
    downmix_filter: str | None = None  # ffmpeg -af filter, used when kind == "downmix"
    channel_fix: str | None = None  # ffmpeg -af filter to remap channel order before transcoding
    source_language: str | None = None  # overrides audio.sourceLanguage for this track only -
    # every title until now only ever produced multiple output tracks (native + downmix) from one
    # source language; a genuine multi-dub source (e.g. two different language tracks, each
    # becoming its own output track) needs each track able to point at its own source stream.
    language_ietf: str | None = None  # optional BCP 47 tag (e.g. "pt-BR") written to Matroska's
    # separate language-ietf element alongside the required ISO 639-2 `language` - verified
    # directly: ffmpeg writes both correctly as distinct elements when given as separate
    # -metadata keys, not one replacing the other. `language` (ISO 639-2, e.g. "por") is what
    # every player reads; this is only for players that use the newer element to distinguish
    # regional variants ISO 639-2 has no separate code for.
    codec: str | None = None  # filter by codec_name (e.g. "dts") when a source has more than one
    # track in the same language - without this, find_stream_index just returns the first
    # language match in stream order, which only happens to pick the lossless track over a lossy
    # duplicate/fallback of the same language if the source happens to order them that way.


@dataclass
class Subtitle:
    language: str
    title: str
    default: bool = False
    source: str = "primary"  # "primary" (a stream in the source file) or "external"
    external_pattern: str | None = None  # only for source == "external"; "{EP}" -> episode tag
    exclude_title_match: str | None = None
    match_title: str | None = None
    codec: str | None = None  # filter by codec_name (e.g. "ass" vs "hdmv_pgs_subtitle")
    language_ietf: str | None = None  # see AudioTrack.language_ietf


@dataclass
class SolarGate:
    """Auto-pauses the video-encode phase (the same pause path a manual toggle uses) whenever
    generation drops below min_watts - job-scheduling concern, not video-specific, hence living at
    the top level of TitleConfig rather than under VideoSettings."""

    enabled: bool
    min_watts: float


@dataclass
class TitleConfig:
    path: Path
    title: str
    source_root: str
    source_folders: list[SourceFolder]
    file_match_regex: str | None
    output_root: str
    naming_transforms: list[NamingTransform]
    naming_append_suffix: str | None
    video: VideoSettings
    audio_source_language: str
    audio_tracks: list[AudioTrack]
    subtitles: list[Subtitle] = field(default_factory=list)
    font_attach_dir: str | None = None
    solar_gate: SolarGate | None = None

    @property
    def settings_summary(self) -> str:
        return f"{self.video.codec} {self.video.preset} preset, CRF{self.video.crf}"


def _parse_source_folder(entry: str | dict) -> SourceFolder:
    """A folder entry is either a plain string (mirrored as-is into output too) or an
    {src, out} object where "out" overrides the output-side subfolder name - "" flattens."""
    if isinstance(entry, str):
        return SourceFolder(src=entry, out=entry)
    return SourceFolder(src=entry["src"], out=entry.get("out", entry["src"]))


def _parse_audio_track(entry: dict) -> AudioTrack:
    return AudioTrack(
        kind=entry["kind"],
        bitrate=entry["bitrate"],
        title=entry["title"],
        language=entry["language"],
        default=entry.get("default", False),
        mapping_family=entry.get("mappingFamily"),
        downmix_filter=entry.get("downmixFilter"),
        channel_fix=entry.get("channelFix"),
        source_language=entry.get("sourceLanguage"),
        language_ietf=entry.get("languageIetf"),
        codec=entry.get("codec"),
    )


def _parse_subtitle(entry: dict) -> Subtitle:
    return Subtitle(
        language=entry["language"],
        title=entry["title"],
        default=entry.get("default", False),
        source=entry.get("source", "primary"),
        external_pattern=entry.get("externalPattern"),
        exclude_title_match=entry.get("excludeTitleMatch"),
        match_title=entry.get("matchTitle"),
        codec=entry.get("codec"),
        language_ietf=entry.get("languageIetf"),
    )


def _parse_deinterlace(entry: dict | None) -> DeinterlaceSettings | None:
    if entry is None:
        return None
    return DeinterlaceSettings(
        tff=entry.get("tff", True),
        fps_divisor=entry.get("fpsDivisor", 2),
        params=entry.get("params", {}),
    )


def _parse_speed_correction(entry: dict | None) -> SpeedCorrection | None:
    if entry is None:
        return None
    return SpeedCorrection(source_fps=entry["sourceFps"], target_fps=entry["targetFps"])


def _parse_solar_gate(entry: dict | None) -> SolarGate | None:
    if entry is None:
        return None
    return SolarGate(enabled=entry.get("enabled", True), min_watts=entry["minWatts"])


def load_config(path: str | Path) -> TitleConfig:
    path = Path(path)
    with path.open() as f:
        data = json.load(f)

    video_data = data["video"]
    video = VideoSettings(
        codec=video_data["codec"],
        preset=video_data["preset"],
        crf=video_data["crf"],
        pix_fmt=video_data.get("pixFmt", ""),
        encoder_params=video_data.get("encoderParams", ""),
        crop=video_data.get("crop"),
        dovi_rpu=video_data.get("doviRpu"),
        chunk_method=video_data.get("chunkMethod", "lsmash"),
        temp_dir=video_data.get("tempDir"),
        deinterlace=_parse_deinterlace(video_data.get("deinterlace")),
        speed_correction=_parse_speed_correction(video_data.get("speedCorrection")),
    )

    source = data["source"]
    naming = data.get("naming", {})
    transforms = [
        NamingTransform(pattern=t["pattern"], replacement=t["replacement"])
        for t in naming.get("transforms", [])
    ]

    audio = data["audio"]

    return TitleConfig(
        path=path,
        title=data["title"],
        source_root=source["root"],
        source_folders=[_parse_source_folder(e) for e in source["folders"]],
        file_match_regex=source.get("fileMatchRegex"),
        output_root=data["output"]["root"],
        naming_transforms=transforms,
        naming_append_suffix=naming.get("appendSuffix"),
        video=video,
        audio_source_language=audio["sourceLanguage"],
        audio_tracks=[_parse_audio_track(t) for t in audio["tracks"]],
        subtitles=[_parse_subtitle(s) for s in data.get("subtitles", [])],
        font_attach_dir=data.get("fontAttachDir"),
        solar_gate=_parse_solar_gate(data.get("solarGate")),
    )
