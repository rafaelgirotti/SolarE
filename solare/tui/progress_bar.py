"""A full-width progress bar with its label centered inside the filled/empty bar itself.

Textual's built-in `ProgressBar` renders percentage as a separate fixed-width widget bolted onto
the end of the bar - it doesn't stretch to fill the row, and there's no way to draw text centered
over the fill. Built as a plain `Static` instead: one string of block characters, with the label
spliced into the middle and colored so it reads clearly whether it sits over the filled or empty
portion.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

_FILLED_CHAR = "█"
_EMPTY_CHAR = "░"


class TextProgressBar(Static):
    def __init__(
        self,
        *,
        filled_color: str,
        empty_color: str,
        label_color: str,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._filled_color = filled_color
        self._empty_color = empty_color
        self._label_color = label_color
        self._progress = 0.0
        self._total = 1.0
        self._label = ""

    def update_progress(self, progress: float, total: float, label: str) -> None:
        self._progress = progress
        self._total = max(total, 1.0)
        self._label = label
        self._render_bar()

    def on_mount(self) -> None:
        self._render_bar()

    def on_resize(self) -> None:
        self._render_bar()

    def _render_bar(self) -> None:
        width = max(self.size.width, 1)
        fraction = min(1.0, max(0.0, self._progress / self._total))
        filled_count = round(width * fraction)

        label = self._label if len(self._label) <= width else self._label[:width]
        label_start = max(0, (width - len(label)) // 2)

        text = Text()
        for i in range(width):
            in_label = label_start <= i < label_start + len(label)
            is_filled = i < filled_count
            if in_label:
                bg = "#1a3a1a" if is_filled else "#000000"
                text.append(label[i - label_start], style=f"bold {self._label_color} on {bg}")
            else:
                char = _FILLED_CHAR if is_filled else _EMPTY_CHAR
                color = self._filled_color if is_filled else self._empty_color
                text.append(char, style=color)

        self.update(text)
