"""Generic yes/no modal confirmation dialog - mouse-clickable buttons, not just keyboard."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmScreen(ModalScreen[bool]):
    """Returns True/False via dismiss() - click Yes/No, or Y/N/Enter/Escape."""

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm_dialog"):
            yield Static(self._message, id="confirm_message")
            with Horizontal(id="confirm_buttons"):
                yield Button("Yes \\[Enter]", id="confirm_yes", variant="success")
                yield Button("No \\[Esc]", id="confirm_no")

    def on_mount(self) -> None:
        self.query_one("#confirm_yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()  # otherwise this bubbles past the modal to SolarEApp's own handler, which
        # doesn't know "confirm_yes"/"confirm_no" and crashes - confirmed live.
        self.dismiss(event.button.id == "confirm_yes")

    def on_key(self, event) -> None:
        if event.key == "y":
            self.dismiss(True)
        elif event.key in ("n", "escape"):
            self.dismiss(False)
