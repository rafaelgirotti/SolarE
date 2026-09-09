"""OpenSubtitles.com API v1 client - downloads a matching subtitle for a queue item as a new
pipeline step (RunPhase.SUBTITLE_FETCH, between AUDIO and MUX - see runner.py).

Two-tier matching, in order:
1. Hash-based lookup (OpenSubtitles' own file-identity hash - file size plus the first/last 64KB
   summed as 64-bit words, the same algorithm Subliminal/VLC/every other hash-matching subtitle
   tool uses; not documented in the REST API docs directly, but it's the same hash the legacy
   XML-RPC API used and the REST API still accepts via the moviehash/moviebytesize params). A hash
   match means the subtitle was uploaded against a file byte-identical to ours, so sync is
   essentially guaranteed. Tried first, always.
2. Falls back to a title+season/episode text search only when no hash match exists - NOT a sync
   guarantee (a different release can have different intro/outro timing for the same episode), so
   this path additionally prefers whichever candidate's own `release` metadata mentions the same
   release type (BluRay/BDRip/WEB-DL/etc.) as our own filename, and logs the fallback distinctly
   rather than presenting it with the same confidence as a hash match.

Auth: a static Api-Key (identifies the app, sufficient for search) plus a JWT from a real account
login (username/password, required specifically for /download) - see
TitleConfig.open_subtitles / OpenSubtitlesCredentials. Anonymous downloads are capped far too low
(~5/day) to be useful for backfilling a whole season.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from solare.engine.config import OpenSubtitlesCredentials, Subtitle, TitleConfig
from solare.engine.queue import QueueItem

try:
    import requests

    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

_BASE_URL = "https://api.opensubtitles.com/api/v1"
_USER_AGENT = "solare v1.0.0"

# OpenSubtitles' own language codes don't match this project's ISO 639-2 `language` field or the
# BCP 47 `language_ietf` field - seeded with only what's actually been needed so far, expand as a
# new title needs a different language rather than pre-populating every ISO code now.
_LANGUAGE_CODES = {"eng": "en"}
_IETF_LANGUAGE_CODES = {"pt-BR": "pt-br"}

_SEASON_EPISODE_RE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
_TRAILING_YEAR_RE = re.compile(r"\s*\(\d{4}\)\s*$")
# Fansub-derived OpenSubtitles uploads sometimes carry leftover ASS/SSA override codes (e.g.
# "{\an8}" for top-center positioning) inside text nominally labeled as plain SRT - confirmed
# live: a real download had these starting a handful of entries in, and whatever player was used
# to test it rendered the first few (tag-free) lines fine, then stopped rendering anything after
# hitting the first "{\...}" sequence it didn't expect - a real, plain-text ASS-in-SRT quality
# problem in the upload itself, not a solare/ffmpeg muxing bug (confirmed separately: extracting
# the embedded track back out of a muxed file showed all entries present and correctly timed all
# the way through - the mux itself loses nothing, a picky player just gives up partway through).
_ASS_OVERRIDE_RE = re.compile(r"\{\\[^}]*\}")
# Common release-type tags found in scene/p2p filenames - used only to prefer a text-search
# candidate whose own `release` field mentions the same type as our source, when a hash match
# isn't available. Not exhaustive - add more as a real title needs one not covered here.
_RELEASE_TOKENS = ["BD-Remux", "BDRemux", "BDRip", "BluRay", "Blu-Ray", "WEB-DL", "WEBRip", "HDTV", "DVDRip"]


def _opensubtitles_language(subtitle: Subtitle) -> str:
    if subtitle.language_ietf and subtitle.language_ietf in _IETF_LANGUAGE_CODES:
        return _IETF_LANGUAGE_CODES[subtitle.language_ietf]
    if subtitle.language in _LANGUAGE_CODES:
        return _LANGUAGE_CODES[subtitle.language]
    raise ValueError(
        f"No OpenSubtitles language code mapping for language={subtitle.language!r} "
        f"languageIetf={subtitle.language_ietf!r} - add one to _LANGUAGE_CODES/_IETF_LANGUAGE_CODES "
        f"in solare/engine/opensubtitles.py"
    )


def _release_token(filename: str) -> str | None:
    for token in _RELEASE_TOKENS:
        if re.search(re.escape(token), filename, re.IGNORECASE):
            return token
    return None


def _best_by_release(results: list[dict], token: str) -> dict:
    for result in results:
        release = result.get("attributes", {}).get("release") or ""
        if re.search(re.escape(token), release, re.IGNORECASE):
            return result
    return results[0]  # no candidate mentions our release type - fall back to the top result


def download_path(out_file: Path, subtitle: Subtitle) -> Path:
    """The predictable on-disk location fetch_subtitle_for_item() downloads to.
    resolve_subtitle_sources() (mux.py) looks for the same path when muxing a
    source=="opensubtitles" entry - both sides must agree on this exact naming convention, hence
    living as one shared function rather than duplicated string formatting in two files."""
    return out_file.parent / f"{out_file.stem}.opensubtitles.{subtitle.language}.srt"


def opensubtitles_hash(file: Path) -> tuple[str, int]:
    """OpenSubtitles' file-identity hash: starts from the file size, then sums the first 64KB and
    last 64KB of the file as 8-byte little-endian unsigned integers into it, wrapping at 2**64.
    Returns (hash_as_16_char_lowercase_hex, filesize) - both required by the /subtitles search
    endpoint's moviehash/moviebytesize params."""
    chunk_size = 65536
    size = file.stat().st_size
    file_hash = size
    mask = 0xFFFFFFFFFFFFFFFF
    with file.open("rb") as f:
        for _ in range(chunk_size // 8):
            file_hash = (file_hash + int.from_bytes(f.read(8), "little")) & mask
        if size > chunk_size:
            f.seek(max(0, size - chunk_size))
            for _ in range(chunk_size // 8):
                data = f.read(8)
                if len(data) < 8:
                    break
                file_hash = (file_hash + int.from_bytes(data, "little")) & mask
    return f"{file_hash:016x}", size


class OpenSubtitlesClient:
    def __init__(self, credentials: OpenSubtitlesCredentials):
        if not _REQUESTS_AVAILABLE:
            raise RuntimeError(
                "requests isn't installed - run `uv sync` to restore it"
            )
        self._credentials = credentials
        self._session = requests.Session()
        self._session.headers.update({"Api-Key": credentials.api_key, "User-Agent": _USER_AGENT})
        self._token: str | None = None

    def _ensure_login(self) -> None:
        if self._token is not None:
            return
        response = self._session.post(
            f"{_BASE_URL}/login",
            json={"username": self._credentials.username, "password": self._credentials.password},
            timeout=30,
        )
        response.raise_for_status()
        self._token = response.json()["token"]
        self._session.headers["Authorization"] = f"Bearer {self._token}"

    def _call(self, method: Callable, url: str, **kwargs) -> requests.Response:
        """Mirrors GrowattClient._call (solare/solar/client.py) - resets the cached JWT on any
        failure so the next call re-logs-in from scratch rather than reusing a token that may have
        expired server-side, instead of failing identically forever."""
        try:
            response = method(url, timeout=30, **kwargs)
            response.raise_for_status()
            return response
        except Exception:
            self._token = None
            raise

    def search_by_hash(self, file: Path, language: str) -> list[dict]:
        file_hash, size = opensubtitles_hash(file)
        response = self._call(
            self._session.get,
            f"{_BASE_URL}/subtitles",
            params={"moviehash": file_hash, "moviebytesize": size, "languages": language},
        )
        return response.json().get("data", [])

    def search_by_query(
        self, query: str, language: str, season: int | None, episode: int | None
    ) -> list[dict]:
        params = {"query": query, "languages": language}
        if season is not None and episode is not None:
            params["season_number"] = season
            params["episode_number"] = episode
        response = self._call(self._session.get, f"{_BASE_URL}/subtitles", params=params)
        return response.json().get("data", [])

    def download(self, file_id: int, dest_path: Path) -> None:
        self._ensure_login()
        response = self._call(self._session.post, f"{_BASE_URL}/download", json={"file_id": file_id})
        link = response.json()["link"]
        subtitle_response = self._session.get(link, timeout=60)
        subtitle_response.raise_for_status()
        text = subtitle_response.content.decode("utf-8")
        dest_path.write_text(_ASS_OVERRIDE_RE.sub("", text), encoding="utf-8")


def fetch_subtitle_for_item(
    config: TitleConfig,
    item: QueueItem,
    subtitle: Subtitle,
    log: Callable[[str], None] = lambda msg: None,
) -> Path:
    """Downloads `subtitle` (source == "opensubtitles") for this queue item, hash-matched against
    the exact source file whenever possible. Idempotent: skips the network entirely if the target
    file already exists (resume-safety, matching item.already_done's own exists() check and
    av1an's own -r resume convention elsewhere in this codebase). Raises on no match or missing
    credentials - this project's established "fail loud, don't silently degrade" convention."""
    dest_path = download_path(item.out_file, subtitle)
    if dest_path.is_file():
        return dest_path

    if config.open_subtitles is None:
        raise ValueError(
            f"Subtitle {subtitle.title!r} has source=opensubtitles but the config has no "
            f"openSubtitles credentials block"
        )
    client = OpenSubtitlesClient(config.open_subtitles)
    language = _opensubtitles_language(subtitle)

    hash_results = client.search_by_hash(item.src_file, language)
    if hash_results:
        result = hash_results[0]
    else:
        match = _SEASON_EPISODE_RE.search(item.src_file.name)
        season, episode = (int(match.group(1)), int(match.group(2))) if match else (None, None)
        query = _TRAILING_YEAR_RE.sub("", config.title)
        query_results = client.search_by_query(query, language, season, episode)
        if not query_results:
            raise ValueError(
                f"No OpenSubtitles match for {item.src_file.name!r} (language={language!r})"
            )
        token = _release_token(item.src_file.name)
        result = _best_by_release(query_results, token) if token else query_results[0]
        log(
            f"subtitle for {item.src_file.name!r} matched by search only, not file hash "
            f"(release token={token!r}) - verify sync manually"
        )

    file_id = result["attributes"]["files"][0]["file_id"]
    client.download(file_id, dest_path)
    return dest_path
