"""Generic yes/no modal confirmation dialog - plain-text choices, clickable and keyboard-drivable.

Deliberately not built from real Button widgets: Button's own default hover/focus/variant CSS
(box border, two-tone fill, bold-reverse focus text) fought this app's flat black-background theme
at every turn and was the source of two real, confirmed visual bugs before landing here - a plain
clickable label sidesteps all of that by construction, and matches this app's plain-text button
hints ("Choose config [C]" etc. are themselves just Button labels, but the effect wanted here -
"Yes"/"No" with no visible box at all - isn't something Button's CSS can be coaxed into without
re-fighting the same battle again on the next Textual upgrade.
"""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


class _ChoiceLabel(Static):
    def __init__(self, text: str, *, resume: bool, **kwargs) -> None:
        super().__init__(text, **kwargs)
        self._resume = resume

    def on_click(self, event: events.Click) -> None:
        self.screen.dismiss(self._resume)


class ConfirmScreen(ModalScreen[bool]):
    """Returns True/False via dismiss() - click Yes/No, or Y/N/Enter/Escape."""

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm_dialog"):
            yield Static(self._message, id="confirm_message")
            with Horizontal(id="confirm_hint"):
                yield _ChoiceLabel("[b]Y[/b]es \\[Enter]     ", resume=True, id="confirm_yes")
                yield _ChoiceLabel("[b]N[/b]o \\[Esc]", resume=False, id="confirm_no")

    def on_key(self, event) -> None:
        if event.key in ("y", "enter"):
            self.dismiss(True)
        elif event.key in ("n", "escape"):
            self.dismiss(False)
