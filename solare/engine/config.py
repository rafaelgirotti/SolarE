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
    encoder_params: str


@dataclass
class TitleConfig:
    path: Path
    title: str
    source_root: str
    output_root: str
    video: VideoSettings

    @property
    def settings_summary(self) -> str:
        return f"{self.video.codec} {self.video.preset} preset, CRF{self.video.crf}"


def load_config(path: str | Path) -> TitleConfig:
    path = Path(path)
    with path.open() as f:
        data = json.load(f)

    video_data = data["video"]
    video = VideoSettings(
        codec=video_data["codec"],
        preset=video_data["preset"],
        crf=video_data["crf"],
        encoder_params=video_data.get("encoderParams", ""),
    )
    return TitleConfig(
        path=path,
        title=data["title"],
        source_root=data["source"]["root"],
        output_root=data["output"]["root"],
        video=video,
    )
