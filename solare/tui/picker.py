"""Modal screen for picking a per-title config .json file."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DirectoryTree, Static


class JsonDirectoryTree(DirectoryTree):
    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [p for p in paths if p.is_dir() or p.suffix == ".json"]


class ConfigPickerScreen(ModalScreen[Path | None]):
    """Returns the chosen path via `dismiss()`, or None if cancelled with Escape."""

    def __init__(self, start_path: str | Path = ".") -> None:
        super().__init__()
        self._start_path = start_path

    def compose(self) -> ComposeResult:
        with Vertical(id="picker_dialog"):
            yield Static("Choose a config file - Esc to cancel", id="picker_title")
            yield JsonDirectoryTree(self._start_path, id="picker_tree")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.dismiss(event.path)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)
