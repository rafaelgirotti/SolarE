"""Combines a plain-text subtitle track's dialogue with a styled ASS reference track's
positioning/formatting, matched by timing - built for OpenSubtitles' plain SRT downloads lacking
the on-screen-text-sign styling/positioning a fansub's own English ASS track already has for the
same source. Not currently wired into the main pipeline (engine/opensubtitles.py) - built and
proven against real episodes of Monster (2004), but left as a standalone, explicitly-invoked step
pending a decision on whether to make it the default treatment for every opensubtitles-sourced
subtitle or only apply it selectively. See history/pitfalls.md for the two real bugs found
building this and history/titles/monster-2004.md for the full investigation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pysubs2

_ASS_OVERRIDE_RE = re.compile(r"\{\\[^}]*\}")
# Matches the override-tag block(s) at the very start of an ASS line's raw text, if any - pysubs2
# keeps override tags embedded in .text, e.g. "{\\an8}Some text".
_LEADING_TAGS_RE = re.compile(r"^(\{\\[^}]*\})+")


def _overlap(a: pysubs2.SSAEvent, b: pysubs2.SSAEvent) -> int:
    return max(0, min(a.end, b.end) - max(a.start, b.start))


def generate_styled_subtitle(reference_ass_path: Path, plain_text_path: Path, out_path: Path) -> dict:
    """Builds a new .ass at out_path: `reference_ass_path`'s full [V4+ Styles] section copied
    verbatim (same fonts - already embedded as mkv attachments if this is muxed back into the same
    file the reference came from, nothing new to add), and one event per line of
    `plain_text_path` (any pysubs2-readable format - .srt, or another .ass with its own tags,
    which get stripped and ignored), each inheriting the style name and any leading positioning
    override tags from whichever reference event it corresponds to.

    Matching is closest-*start*-time among time-overlapping candidates, not maximum-overlap -
    found live to matter for a real "accumulating reveal" pattern (a run of on-screen-text lines
    each with its own distinct start time and hand-placed \\pos, but all sharing one shared *end*
    time so they visually stack together): maximum-overlap collapses every one of them onto
    whichever single candidate has the longest raw overlap (usually the first/longest-spanning
    one), so every line in the group inherited the identical position - confirmed live, this
    produced a visibly static result overlapping the video's own on-screen text instead of each
    line taking its own correct spot. Closest-start correctly pairs each line with its own
    corresponding reveal step instead.

    Explicitly writes LF-only line endings, not pysubs2's own CRLF default - confirmed live as a
    real, previously-unexplained mpv-only rendering failure: a CRLF-terminated .ass muxed into
    Matroska played back correctly for exactly one event before mpv silently discarded the real
    persistent-ASS header for every subsequent packet, reinitializing per-line with a generic
    FFmpeg-synthesized fallback header (tiny default resolution, one plain style) and switching to
    its SRT-decoder code path - not a mux/data-loss bug (ffprobe's own re-extraction, and a plain
    mkvmerge self-remux, both read the file back perfectly correctly either way; this only shows
    up in mpv's real playback path). The original English ASS this project always sources styling
    from is LF-terminated (Aegisub's own default) - matching that convention here, rather than
    whatever pysubs2's platform default happens to be, avoids the whole class of bug rather than
    just this one instance of it.

    Returns a small dict of counters ({total, matched_signs, fallback_default}) for the caller to
    log - not raised as an error condition, since a line with no time-overlapping reference event
    falling back to the plain default style is an expected, non-fatal edge case (confirmed live:
    ~2-3% of lines in a typical episode, usually at scene boundaries the two sources timed
    slightly differently).
    """
    reference = pysubs2.load(str(reference_ass_path))
    plain = pysubs2.load(str(plain_text_path))

    out = pysubs2.SSAFile()
    out.info.update(reference.info)
    out.styles = dict(reference.styles)  # verbatim copy - same fonts/colors/margins/sizes

    counters = {"total": 0, "matched_signs": 0, "fallback_default": 0}
    for line_in in plain:
        candidates = [e for e in reference if _overlap(e, line_in) > 0]
        best = min(candidates, key=lambda e: abs(e.start - line_in.start), default=None)
        clean_text = _ASS_OVERRIDE_RE.sub("", line_in.text)
        line_out = pysubs2.SSAEvent(start=line_in.start, end=line_in.end)
        if best is not None:
            line_out.style = best.style
            tags = _LEADING_TAGS_RE.match(best.text)
            line_out.text = (tags.group(0) if tags else "") + clean_text
            if best.style == "signs":
                counters["matched_signs"] += 1
        else:
            line_out.style = "Default"
            line_out.text = clean_text
            counters["fallback_default"] += 1
        out.append(line_out)
        counters["total"] += 1

    # pysubs2's .save() writes the platform line ending (CRLF on Windows) - force LF regardless,
    # see the docstring above for why this matters for real mpv playback, not just cosmetics.
    raw = "\n".join(out.to_string("ass").splitlines()) + "\n"
    Path(out_path).write_text(raw, encoding="utf-8")
    return counters
