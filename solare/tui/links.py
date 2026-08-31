"""Clickable terminal hyperlinks (OSC 8, via a Style(link=...) applied directly to a Content
object) and path shortening.

The visible text stays short (a filename, or a shortened path) while the link target is always
the full real path - so a long path never has to be displayed in full to remain clickable.
"""

from __future__ import annotations

from pathlib import Path

from textual.content import Content
from textual.style import Style


def shorten_path(path_str: str, keep_tail: int = 2) -> str:
    """Keep the root plus the last `keep_tail` components, collapsing the middle to '...'."""
    parts = Path(path_str).parts
    if len(parts) <= keep_tail + 1:
        return path_str
    return str(Path(parts[0], "...", *parts[-keep_tail:]))


def hyperlink(display: str, real_path: str) -> Content:
    """Wrap `display` in a clickable hyperlink pointing at `real_path`, as a `Content` object
    built directly rather than via markup-string interpolation (`[link="..."]{display}[/link]`,
    the previous approach) - `display` is very often a real filename or path, which can contain
    characters (an unmatched `[`, in particular - confirmed live via a real crash) that Textual's
    markup parser mishandles once glued into a larger string still meant to go through
    `Content.from_markup()`. `Content(display)` never runs it through that parser at all, so
    there's nothing for it to misinterpret regardless of what `display` contains.

    Falls back to plain `display` (still as literal, unlinked `Content`) if the path can't be
    turned into a file:// URI (e.g. it's not an absolute path) rather than raising - a broken
    link is worse than no link.
    """
    content = Content(display)
    try:
        url = Path(real_path).resolve().as_uri()
    except (ValueError, OSError):
        return content
    return content.stylize(Style(link=url))
