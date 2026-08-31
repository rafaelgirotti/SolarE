"""Job queue building: scans config.source.folders, matches files, computes output paths.

Skip-if-output-exists is left to the caller (via QueueItem.already_done) rather than filtered out
here - the queue includes every matched file regardless of whether its output exists yet, so a
resumed run's log can still show what it skipped and why, checked per-item right before actually
processing it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from solare.engine.config import TitleConfig


@dataclass
class QueueItem:
    src_file: Path
    out_file: Path
    log_base: Path

    @property
    def already_done(self) -> bool:
        return self.out_file.exists()


def clean_title(config: TitleConfig, src_file: Path) -> str:
    """Apply config.naming.transforms (ordered regex replacements) to the source filename's stem.

    Only the `transforms` naming schema is implemented - a plain find/replace schema and an
    episode-number-to-canonical-title lookup schema are plausible future additions, but
    config.example.json doesn't document either yet, so there's nothing real to build them
    against.
    """
    base = src_file.stem
    for transform in config.naming_transforms:
        base = re.sub(transform.pattern, transform.replacement, base)
    return base


def output_name(config: TitleConfig, src_file: Path) -> str:
    base = clean_title(config, src_file)
    if config.naming_append_suffix:
        base += config.naming_append_suffix
    return f"{base}.mkv"


def build_queue(config: TitleConfig) -> list[QueueItem]:
    """Scan every config.source.folders entry, matching files and computing output paths.

    Raises FileNotFoundError if any source folder is missing, rather than silently queuing zero
    work - a config pointing at nothing (a stale path, a title already moved/deleted) is almost
    always a mistake worth stopping for immediately, not discovering later from an empty queue.
    """
    items: list[QueueItem] = []
    source_root = Path(config.source_root)
    output_root = Path(config.output_root)

    for folder_entry in config.source_folders:
        src_folder = source_root / folder_entry.src
        out_folder = output_root / folder_entry.out if folder_entry.out else output_root

        if not src_folder.is_dir():
            raise FileNotFoundError(
                f"Source folder does not exist: {src_folder} "
                f"(from source.root={config.source_root!r} + folder {folder_entry.src!r}) - "
                f"check the config is pointing at the right title, and the source hasn't already "
                f"been moved or deleted."
            )
        out_folder.mkdir(parents=True, exist_ok=True)

        files = sorted(src_folder.glob("*.mkv"), key=lambda p: p.name)
        if config.file_match_regex:
            pattern = re.compile(config.file_match_regex)
            files = [f for f in files if pattern.search(f.name)]

        for f in files:
            items.append(
                QueueItem(
                    src_file=f,
                    out_file=out_folder / output_name(config, f),
                    log_base=out_folder / (re.sub(r"[^\w]", "_", f.stem) + ".encode-log.txt"),
                )
            )

    if not items:
        raise RuntimeError(
            "Queue is empty after scanning all source.folders - every source file already has a "
            "matching output, or fileMatchRegex matched nothing."
        )
    return items


def has_unfinished_work(config: TitleConfig) -> bool:
    """True only when the queue resolves cleanly and at least one matched source file's output
    doesn't exist yet - used to decide whether a "resume last job?" startup prompt is worth
    showing. Any ambiguous case (source folder since moved/deleted, or every file already done)
    returns False rather than guessing - a false negative here just means no prompt, not an
    incorrectly confident one."""
    try:
        items = build_queue(config)
    except (FileNotFoundError, RuntimeError):
        return False
    return any(not item.already_done for item in items)
