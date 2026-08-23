"""Clickable terminal hyperlinks (OSC 8, via Rich's `[link=...]` markup) and path shortening.

The visible text stays short (a filename, or a shortened path) while the link target is always
the full real path - so a long path never has to be displayed in full to remain clickable.
"""

from __future__ import annotations

from pathlib import Path


def shorten_path(path_str: str, keep_tail: int = 2) -> str:
    """Keep the root plus the last `keep_tail` components, collapsing the middle to '...'."""
    parts = Path(path_str).parts
    if len(parts) <= keep_tail + 1:
        return path_str
    return str(Path(parts[0], "...", *parts[-keep_tail:]))


def hyperlink(display: str, real_path: str) -> str:
    """Wrap `display` in a clickable hyperlink pointing at `real_path`.

    Falls back to plain `display` if the path can't be turned into a file:// URI (e.g. it's not
    an absolute path) rather than raising - a broken link is worse than no link. The URL is
    quoted: Textual's markup grammar only accepts a bare word or a quoted string as a tag value,
    not arbitrary punctuation like `://` unquoted.
    """
    try:
        url = Path(real_path).resolve().as_uri()
    except (ValueError, OSError):
        return display
    return f'[link="{url}"]{display}[/link]'
