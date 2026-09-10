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
_FS_TAG_RE = re.compile(r"\\fs([\d.]+)")
# Fallback only for a file with no "Default" style at all to compare against (shouldn't happen in
# practice - every reference track in this project has one) - see _standard_styles().
_FALLBACK_STANDARD_STYLES = {"Default", "Default - Italic"}
# Ending/opening theme song lyrics - never auto-embedded (translated OR verbatim) into a generated
# subtitle: unlike a name card or location sign, lyrics are unambiguously copyrighted creative
# text, not a proper noun with no real translation decision to make. A reference event in one of
# these styles is treated as if it doesn't exist at all - never matched to a plain line, never
# preserved as an "untranslated" orphan - so the ending song simply plays without a caption for
# these specific lines rather than this tool ever generating or reproducing lyric text itself.
_LYRIC_STYLES = {"ED", "ED-ENG"}


def _standard_styles(styles: dict) -> set[str]:
    """Determines which style names represent plain spoken dialogue (as opposed to a hand-placed
    on-screen-text overlay), by comparing each style's own (Fontname, Fontsize) against the
    "Default" style's - confirmed live as necessary, not just more robust in theory: episode 2's
    reference introduces "Default - Italic an8" (identical to "Default - Italic" - same Gandhi
    Sans 75 - except Alignment 8/top instead of 2/bottom, presumably a phone-call or narration line
    placed above whatever's normally at the bottom of that frame), a real ordinary dialogue
    variant that a fixed set of known names (this project's original approach) would have wrongly
    flagged as needing frame-by-frame position review, since it had never been seen before. This
    source always gives on-screen text (signs/credits/karaoke) a completely different, often
    decorative font from spoken dialogue (Arial/Souvenir Lt BT/AvantGarde Md BT/X-Files/etc. vs
    Gandhi Sans) - matching font+size against Default is a reliable, self-adapting signal
    regardless of what a given episode's own fansubber happened to name a dialogue variant."""
    if "Default" not in styles:
        return set(_FALLBACK_STANDARD_STYLES)
    default = styles["Default"]
    return {
        name for name, style in styles.items()
        if style.fontname == default.fontname and style.fontsize == default.fontsize
    }


def _overlap(a: pysubs2.SSAEvent, b: pysubs2.SSAEvent) -> int:
    return max(0, min(a.end, b.end) - max(a.start, b.start))


def _fmt_ms(ms: int) -> str:
    h, rem = divmod(max(ms, 0), 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1_000)
    return f"{h:d}:{m:02d}:{s:02d}.{ms:03d}"


def _layer_group(reference_events: list[pysubs2.SSAEvent], anchor: pysubs2.SSAEvent) -> list[pysubs2.SSAEvent]:
    """Every reference event sharing `anchor`'s style AND exact start/end - confirmed live as how
    this source builds a shadow-plus-foreground on-screen-text effect (e.g. Monster's Dusseldorf
    location card: a solid-black Layer 1 event underneath a near-white Layer 2 event, same style/
    timing, different colour/blur). A single translated line has to reproduce every layer in the
    group, not just whichever one happened to be picked as the timing match - matching only the
    black shadow layer renders solid black text with no white foreground on top of it at all."""
    return sorted(
        (e for e in reference_events if e.style == anchor.style and e.start == anchor.start and e.end == anchor.end),
        key=lambda e: e.layer,
    )


def _sanitize_onscreen_text(text: str) -> str:
    """A colon in on-screen text styled with certain custom fonts can trigger a broken/missing
    glyph - confirmed live on Monster's "X-Files" display font (used for its opening verse card):
    a colon anywhere in the string made HarfBuzz's shaping drop every character *before* it in the
    same run, not just render a tofu box for the colon itself - "APOCALIPSE 13: 1-4" displayed as
    only "1-4", and the user reproduced the identical failure live in Aegisub with unrelated text
    ("TESTE: 12-3" -> "12-3"), confirming it's the character, not this specific string. A comma
    reads acceptably in its place and is already proven to render correctly in every other
    on-screen-text line in this source. Applied to every non-standard-style line generated here,
    not just the one this was first found on - we don't know which other decorative fonts in this
    or other titles share the same defect, and a colon is rare enough in on-screen text that a
    blanket substitution costs nothing."""
    return text.replace(":", ",")


def _insert_tag(tags: str, tag: str) -> str:
    idx = tags.rfind("}")
    return tags[:idx] + tag + tags[idx:] if idx != -1 else "{" + tag + "}" + tags


def _shrink_to_fit(tags: str, style_fontsize: float, source_plain: str, translated_plain: str) -> str:
    """On-screen text is hand-positioned against one specific frame with just enough room for the
    *source* line's own length - confirmed live as a real, separate bug from the CRLF/timing ones:
    a Portuguese line meaningfully longer than its English counterpart wrapped to an extra line
    inside the same tight slot, overflowing into the very on-screen text (a fixed, immovable part
    of the video) it was supposed to sit *between*. Shrinking the font in proportion to how much
    longer the translation is keeps it inside the space the source line actually fit in - capped at
    1.6x so a wildly longer translation gets a legibility floor instead of shrinking to nothing
    (still flagged either way for a human to check)."""
    ratio = len(translated_plain) / max(1, len(source_plain))
    if ratio <= 1.05:
        return tags
    scale = min(ratio, 1.6)
    match = _FS_TAG_RE.search(tags)
    base_fs = float(match.group(1)) if match else style_fontsize
    new_tag = f"\\fs{base_fs / scale:.2f}"
    return tags[: match.start()] + new_tag + tags[match.end() :] if match else _insert_tag(tags, new_tag)


def find_nonstandard_events(ass_path: Path) -> list[dict]:
    """Lists every event in `ass_path` whose style isn't plain spoken dialogue (see _standard_styles())
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
    standard_styles = _standard_styles(subs.styles)
    flagged = [
        {
            "start": _fmt_ms(e.start),
            "end": _fmt_ms(e.end),
            "style": e.style,
            "text": _LEADING_TAGS_RE.sub("", e.text).replace("\\N", " "),
        }
        for e in subs
        if e.style not in standard_styles
    ]
    return sorted(flagged, key=lambda f: f["start"])


def find_duplicate_captions(ass_path: Path, window_ms: int = 5000) -> list[dict]:
    """Flags a plain-dialogue (see _standard_styles()) event whose text closely matches a nearby
    non-standard-style event's text - confirmed live as a real, recurring OpenSubtitles pattern on
    this source: the plain-text translation sometimes transcribes an on-screen name/location card
    as if it were spoken dialogue (standalone "EVA HEINEMANN" / "DR. BECKER" lines), duplicating
    what the matched or orphan-preserved signs card already displays - found live via the user's
    own read-through after the "prefer standard style" matching fix (which fixed the *category*
    mismatch bug but does nothing about a genuinely separate, redundant plain-text line that was
    never wrongly matched to begin with). Only short dialogue lines (at most 4 words after
    stripping punctuation - long enough that a real sentence won't false-positive against a bare
    name) within `window_ms` of a non-standard event with matching (case-folded,
    punctuation-stripped) text are flagged. Returns candidates for a human to confirm before
    deleting - a short line that coincidentally repeats a name as real dialogue is possible, if
    rare, so this doesn't delete anything itself."""
    subs = pysubs2.load(str(ass_path))
    standard_styles = _standard_styles(subs.styles)
    nonstandard = [e for e in subs if e.style not in standard_styles]

    def normalize(text: str) -> str:
        plain = _LEADING_TAGS_RE.sub("", text).replace("\\N", " ").strip().casefold()
        return re.sub(r"[^\w\s]", "", plain)

    duplicates = []
    for d in subs:
        if d.style not in standard_styles:
            continue
        d_norm = normalize(d.text)
        if not d_norm or len(d_norm.split()) > 4:
            continue
        for n in nonstandard:
            if abs(n.start - d.start) > window_ms:
                continue
            if d_norm == normalize(n.text):
                duplicates.append({
                    "start": _fmt_ms(d.start), "end": _fmt_ms(d.end),
                    "text": _LEADING_TAGS_RE.sub("", d.text).replace("\\N", " "),
                    "duplicates_style": n.style, "duplicates_at": _fmt_ms(n.start),
                })
                break
    return duplicates


_POS_RE = re.compile(r"\\pos\((-?[\d.]+),(-?[\d.]+)\)")


def find_position_collisions(ass_path: Path, distance_px: float = 60.0) -> list[dict]:
    """Flags two non-standard-style events that overlap in time AND sit within `distance_px` of
    each other's `\\pos` - a genuine visual collision (two separate captions landing on top of one
    another), as opposed to the intentional shadow-plus-foreground layer pairs `_layer_group`
    builds (same style, same exact start/end, same position, by design - explicitly excluded here
    rather than flagged). Only compares events with an explicit `\\pos` - overlapping plain
    dialogue uses the style's own shared position, which is normal/expected stacking behavior, not
    a collision. Returns candidates for a human to check against the actual frame, same as
    find_nonstandard_events - a small on-screen distance isn't proof of a real problem (Monster's
    own reveal-card lines sit ~85px apart by design), just something worth a look."""
    subs = pysubs2.load(str(ass_path))
    standard_styles = _standard_styles(subs.styles)
    positioned = []
    for e in subs:
        if e.style in standard_styles:
            continue
        m = _POS_RE.search(e.text)
        if m:
            positioned.append((e, float(m.group(1)), float(m.group(2))))

    collisions = []
    seen_pairs = set()
    for i, (a, ax, ay) in enumerate(positioned):
        for b, bx, by in positioned[i + 1 :]:
            if _overlap(a, b) <= 0:
                continue
            if a.style == b.style and a.start == b.start and a.end == b.end:
                continue  # intentional layer pair
            if ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 > distance_px:
                continue
            key = tuple(sorted([(a.start, a.text), (b.start, b.text)]))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            collisions.append({
                "a": {"start": _fmt_ms(a.start), "style": a.style, "text": _LEADING_TAGS_RE.sub("", a.text)},
                "b": {"start": _fmt_ms(b.start), "style": b.style, "text": _LEADING_TAGS_RE.sub("", b.text)},
                "distance_px": round(((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5, 1),
            })
    return collisions


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

    Among time-overlapping candidates, a plain standard-style (dialogue) candidate is always
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

    For a matched on-screen-text style (anything outside the standard-style set - signs, credits,
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

    Two more real, separate on-screen-text bugs fixed here, found by actually burning the output
    onto real frames rather than trusting timestamps/coordinates alone (see
    history/pitfalls.md): a reference sign built from multiple stacked events at the identical
    style+start+end (a shadow-colour layer under a near-white foreground layer, this source's own
    technique for a legible caption against a busy background) needs every layer reproduced, not
    just whichever one the timing match happened to land on - see _layer_group. And a reference
    on-screen-text event with no plain-text line ever time-overlapping it at all (typically a
    character name card - nobody speaks a name aloud) is preserved verbatim, untranslated, in a
    pass after the main loop, rather than silently vanishing from the output entirely - EXCEPT for
    _LYRIC_STYLES (ending/opening theme lyrics), which are excluded from every step above, not just
    this one: unlike a name or location, lyrics are unambiguously copyrighted creative text, so this
    function never matches a plain line to one, never preserves one untranslated, never reproduces
    lyric text in the generated file at all - those specific lines simply have no caption.

    Each matched (non-orphan) flagged entry also carries "suspicious_match": True when the
    translated text's length is more than 1.6x or less than 0.625x (1/1.6) its matched reference
    line's own length - the same 1.6x this module already treats as the outer limit
    `_shrink_to_fit` can compensate for, so this flags exactly the cases that heuristic can no
    longer fully paper over. Found live as a real, separate content-loss bug: a plain SRT line
    with no genuine reference counterpart at all (a translator's own end-credit, unrelated to
    anything in the English track) still has to match *something* since the pool is never empty
    as long as one on-screen-text event exists nearby, and it silently overwrote a real title card
    ("The Missing") that happened to share its timing - the credit's own text (28 chars) was 2.5x
    the title's (11 chars). A second real case (episode 10, "511 Kinderheim" vs. the same 28-char
    credit) landed at exactly 2.0x - confirmed live that an earlier 2.0x/0.5x threshold missed it
    outright, which is why this uses the tighter, principled 1.6x boundary instead of an arbitrary
    round number. A ratio this far off is a strong hint the match itself is wrong, not just that
    the translation runs long - surfaced for extra scrutiny during review rather than silently
    accepted.

    Returns a dict of counters ({total, matched_signs, fallback_default, nonstandard_position,
    suspicious_match}) for the caller to log, plus "flagged": the same list
    find_nonstandard_events() would produce on the
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

    # Comment-type events (a fansubber's own private typesetting notes, e.g. "#ref 01" - confirmed
    # live as real content in this source's NCOP entries) were never meant to display at all;
    # lyric-style events are excluded per _LYRIC_STYLES above. Treating both as absent from the
    # reference entirely - rather than filtering them out separately in each place `reference` gets
    # iterated - keeps every one of matching/layer-grouping/orphan-preservation below correct by
    # construction instead of by remembering to re-check both conditions everywhere.
    reference_events = [e for e in reference if not e.is_comment and e.style not in _LYRIC_STYLES]
    standard_styles = _standard_styles(reference.styles)

    counters = {"total": 0, "matched_signs": 0, "fallback_default": 0, "nonstandard_position": 0, "suspicious_match": 0}
    flagged: list[dict] = []
    used_keys: set[tuple] = set()
    for line_in in plain:
        candidates = [e for e in reference_events if _overlap(e, line_in) > 0]
        standard_candidates = [e for e in candidates if e.style in standard_styles]
        pool = standard_candidates or candidates
        best = min(pool, key=lambda e: abs(e.start - line_in.start), default=None)
        clean_text = _ASS_OVERRIDE_RE.sub("", line_in.text)
        counters["total"] += 1

        if best is None:
            out.append(pysubs2.SSAEvent(start=line_in.start, end=line_in.end, style="Default", text=clean_text))
            counters["fallback_default"] += 1
            continue

        if best.style in standard_styles:
            tags = _LEADING_TAGS_RE.match(best.text)
            line_out = pysubs2.SSAEvent(start=line_in.start, end=line_in.end, style=best.style)
            line_out.text = (tags.group(0) if tags else "") + clean_text
            out.append(line_out)
            continue

        # On-screen text (signs/credits/karaoke): copy the reference's own timing (see docstring),
        # collapse the plain source's own dialogue-oriented line break (built for a normal subtitle
        # box, not a small hand-positioned caption), shrink to fit if the translation runs
        # meaningfully longer than the source line it's replacing, and reproduce every stacked
        # layer, not just whichever one the timing match happened to land on.
        if best.style == "signs":
            counters["matched_signs"] += 1
        used_keys.add((best.style, best.start, best.end))
        translated_plain = _sanitize_onscreen_text(clean_text.replace("\\N", " "))
        source_plain = _LEADING_TAGS_RE.sub("", best.text).replace("\\N", " ")
        style_fontsize = reference.styles[best.style].fontsize if best.style in reference.styles else 27.0
        for member in _layer_group(reference_events, best):
            tags_match = _LEADING_TAGS_RE.match(member.text)
            tags = _shrink_to_fit(tags_match.group(0) if tags_match else "{}", style_fontsize, source_plain, translated_plain)
            event = pysubs2.SSAEvent(start=best.start, end=best.end, style=member.style, layer=member.layer)
            event.text = tags + translated_plain
            out.append(event)
        counters["nonstandard_position"] += 1
        length_ratio = len(translated_plain) / max(1, len(source_plain))
        flagged.append({
            "start": _fmt_ms(best.start), "end": _fmt_ms(best.end),
            "style": best.style, "text": translated_plain,
            "suspicious_match": length_ratio > 1.6 or length_ratio < 0.625,
        })
        if length_ratio > 1.6 or length_ratio < 0.625:
            counters["suspicious_match"] += 1

    # Preserve on-screen-text reference events with no corresponding translated line at all - e.g.
    # a character name card, since nobody speaks a name aloud so no plain-text line will ever
    # time-overlap it. Confirmed live as a real gap once the cross-category mismatch fix above
    # stopped letting a nearby dialogue line steal a name card's slot: two name cards vanished from
    # the output entirely rather than picking up a translation, since the loop above only ever
    # visits reference events that some plain line actually matched. Untranslated (the source's own
    # text, verbatim) is still far better than silently absent - flagged either way for follow-up.
    seen_orphan_keys: set[tuple] = set()
    for e in reference_events:
        if e.style in standard_styles:
            continue
        key = (e.style, e.start, e.end)
        if key in used_keys or key in seen_orphan_keys:
            continue
        seen_orphan_keys.add(key)
        for member in _layer_group(reference_events, e):
            member_tags_match = _LEADING_TAGS_RE.match(member.text)
            member_tags = member_tags_match.group(0) if member_tags_match else ""
            member_visible = _sanitize_onscreen_text(member.text[len(member_tags):])
            out.append(pysubs2.SSAEvent(
                start=member.start, end=member.end, style=member.style, layer=member.layer,
                text=member_tags + member_visible,
            ))
        counters["nonstandard_position"] += 1
        flagged.append({
            "start": _fmt_ms(e.start), "end": _fmt_ms(e.end), "style": e.style,
            "text": "[untranslated] " + _sanitize_onscreen_text(_LEADING_TAGS_RE.sub("", e.text).replace("\\N", " ")),
            "suspicious_match": False,
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
