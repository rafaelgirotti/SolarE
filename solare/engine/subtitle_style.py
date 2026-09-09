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
# Plain spoken dialogue, rendered at the style's own default bottom-center box with no per-line
# positioning - every other style in this project's Monster (2004) reference tracks (signs, ED,
# ED-ENG, NCOP, X-Files) is a hand-placed on-screen-text overlay (credits, karaoke, signage) baked
# to a specific spot in a specific frame, not something a timing/text swap alone can validate is
# still correctly placed for different (usually longer) translated text - see find_nonstandard_events.
_STANDARD_STYLES = {"Default", "Default - Italic"}


def _overlap(a: pysubs2.SSAEvent, b: pysubs2.SSAEvent) -> int:
    return max(0, min(a.end, b.end) - max(a.start, b.start))


def _fmt_ms(ms: int) -> str:
    h, rem = divmod(max(ms, 0), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1_000)
    return f"{h:d}:{m:02d}:{s:02d}.{ms:03d}"


def find_nonstandard_events(ass_path: Path) -> list[dict]:
    """Lists every event in `ass_path` whose style isn't plain spoken dialogue (_STANDARD_STYLES)
    - i.e. every hand-placed on-screen-text overlay a fansub timed/positioned against one specific
    source video. A timing/text-matching heuristic (generate_styled_subtitle) can get the *style*
    right while still landing the actual translated text somewhere that doesn't fit the frame it
    was built for (overlapping the video's own on-screen text, running off-screen, etc.) - style
    name alone isn't proof of correct placement, only that it was intended to be non-default.
    Meant to be checked every time a styled subtitle is generated (not just once, by hand) so
    nothing "not standard" slips through unreviewed - see the caller's own log output for how this
    is meant to be surfaced. Returns dicts with start/end (human-readable timestamps), style, and
    the plain text, ordered by start time, for a human to jump to each timestamp and confirm
    placement against the actual frame."""
    subs = pysubs2.load(str(ass_path))
    flagged = [
        {
            "start": _fmt_ms(e.start),
            "end": _fmt_ms(e.end),
            "style": e.style,
            "text": _LEADING_TAGS_RE.sub("", e.text).replace("\\N", " "),
        }
        for e in subs
        if e.style not in _STANDARD_STYLES
    ]
    return sorted(flagged, key=lambda f: f["start"])


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

    Among time-overlapping candidates, a plain _STANDARD_STYLES (dialogue) candidate is always
    preferred over a non-standard one, even when a non-standard candidate's start is numerically
    closer - confirmed live as a real, separate mismatch: a spoken line ("Although... it seems
    you're not just good in surgery") landed 0.06s after a same-moment on-screen name-card event
    ("Dr. Becker", style "signs") starts, closer in absolute time than the correct Default-style
    reference event was - so plain closest-start alone handed an ordinary spoken line the name
    card's own cramped bottom-of-frame position and (once matched) its timing, instead of the
    normal subtitle spot. A character name card appearing right as that character starts speaking
    is common in this source, not a one-off - restricting the pool to standard-style candidates
    first (falling back to the full candidate set only when none exist) avoids this whole category
    of mismatch rather than special-casing this one instance of it.

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

    For a matched on-screen-text style (anything outside _STANDARD_STYLES - signs, credits,
    karaoke), the OUTPUT event's start/end is copied from `best` (the matched reference event)
    directly, not the incoming plain-text line's own timing - confirmed live as a real, separate
    bug from the CRLF one, on an "accumulating reveal" sequence (see the closest-start-time
    matching note above): OpenSubtitles' own plain-text translation already has each line
    individually staggered close to the reference's own per-line start (so closest-start matching
    still pairs each line with its own correct reveal step), but ends each line shortly after -
    unlike the reference, where every line in the group shares one common *end* far later, keeping
    every already-revealed line accumulated on screen together until the whole passage fades out
    as a block. Keeping the plain line's own short end faithfully reproduced each line individually
    appearing and disappearing in turn instead of building up - never showing the complete
    assembled passage at once. Copying `best.start`/`best.end` verbatim (not some group-wide
    reduction across every reference event sharing that end - `best` already carries the correct
    individual start *and* the correct shared end, no extra lookup needed) restores both: each
    translated line still appears at its own correct staggered moment, and stays accumulated
    through the group's real shared end instead of vanishing early. Plain spoken dialogue keeps its
    own SRT timing unconditionally - a translation naturally reads at different pacing than the
    source language, which is exactly what a translator timed that track for; only video-timed
    on-screen text should ever be forced to match the source's own clock.

    Returns a dict of counters ({total, matched_signs, fallback_default, nonstandard_position}) for
    the caller to log, plus "flagged": the same list find_nonstandard_events() would produce on the
    output file, so a caller always gets it back without a second pass - `nonstandard_position`
    counting an event with no time-overlapping reference match as fallback_default (0 additions
    there), not raised as an error condition, since a line with no time-overlapping reference event
    falling back to the plain default style is an expected, non-fatal edge case (confirmed live:
    ~2-3% of lines in a typical episode, usually at scene boundaries the two sources timed
    slightly differently).
    """
    reference = pysubs2.load(str(reference_ass_path))
    plain = pysubs2.load(str(plain_text_path))

    out = pysubs2.SSAFile()
    out.info.update(reference.info)
    out.styles = dict(reference.styles)  # verbatim copy - same fonts/colors/margins/sizes

    counters = {"total": 0, "matched_signs": 0, "fallback_default": 0, "nonstandard_position": 0}
    flagged: list[dict] = []
    for line_in in plain:
        candidates = [e for e in reference if _overlap(e, line_in) > 0]
        standard_candidates = [e for e in candidates if e.style in _STANDARD_STYLES]
        pool = standard_candidates or candidates
        best = min(pool, key=lambda e: abs(e.start - line_in.start), default=None)
        clean_text = _ASS_OVERRIDE_RE.sub("", line_in.text)
        line_out = pysubs2.SSAEvent(start=line_in.start, end=line_in.end)
        if best is not None:
            line_out.style = best.style
            tags = _LEADING_TAGS_RE.match(best.text)
            line_out.text = (tags.group(0) if tags else "") + clean_text
            if best.style not in _STANDARD_STYLES:
                line_out.start, line_out.end = best.start, best.end
            if best.style == "signs":
                counters["matched_signs"] += 1
        else:
            line_out.style = "Default"
            line_out.text = clean_text
            counters["fallback_default"] += 1
        out.append(line_out)
        counters["total"] += 1
        if line_out.style not in _STANDARD_STYLES:
            counters["nonstandard_position"] += 1
            flagged.append({
                "start": _fmt_ms(line_out.start),
                "end": _fmt_ms(line_out.end),
                "style": line_out.style,
                "text": _LEADING_TAGS_RE.sub("", line_out.text).replace("\\N", " "),
            })
    counters["flagged"] = flagged

    # pysubs2's .save() writes the platform line ending (CRLF on Windows) - force LF regardless,
    # see the docstring above for why this matters for real mpv playback, not just cosmetics.
    # `newline="\n"` here is not optional decoration: Path.write_text()'s own universal-newline
    # handling re-translates every "\n" in `raw` back to os.linesep (CRLF on Windows) by default,
    # on WRITE as well as read - confirmed live, this silently defeated the LF-only join above on
    # every previous attempt (verified via raw byte inspection: the file on disk had \r\n even
    # though `raw` itself, in memory, never contained \r). `newline="\n"` (equivalently `newline=""`)
    # disables that translation so the bytes actually written match what was joined.
    raw = "\n".join(out.to_string("ass").splitlines()) + "\n"
    Path(out_path).write_text(raw, encoding="utf-8", newline="\n")
    return counters
