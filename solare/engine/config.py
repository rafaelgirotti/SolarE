"""Per-title job configuration - parses the schema in config/config.example.json.

No validation framework: a missing required field raises a plain KeyError with the field name in
it, which is enough for a config file the user wrote by hand and can fix immediately - this isn't
attacker-controlled input that needs defensive parsing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class VideoSettings:
    codec: str
    preset: str
    crf: str
    pix_fmt: str
    encoder_params: str


@dataclass
class SourceFolder:
    src: str
    out: str  # "" flattens straight into output.root instead of mirroring a subfolder


@dataclass
class NamingTransform:
    pattern: str
    replacement: str


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

    @property
    def settings_summary(self) -> str:
        return f"{self.video.codec} {self.video.preset} preset, CRF{self.video.crf}"


def _parse_source_folder(entry: str | dict) -> SourceFolder:
    """A folder entry is either a plain string (mirrored as-is into output too) or an
    {src, out} object where "out" overrides the output-side subfolder name - "" flattens."""
    if isinstance(entry, str):
        return SourceFolder(src=entry, out=entry)
    return SourceFolder(src=entry["src"], out=entry.get("out", entry["src"]))


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
    )

    source = data["source"]
    naming = data.get("naming", {})
    transforms = [
        NamingTransform(pattern=t["pattern"], replacement=t["replacement"])
        for t in naming.get("transforms", [])
    ]

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
    )
