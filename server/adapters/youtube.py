"""YouTube lecture → transcript source, from the video's own published captions.

Never invents a transcript: if the video has no captions (or YouTube refuses the request), the caller gets an error
and the user can paste a transcript instead. Uses youtube-transcript-api (an unofficial client of YouTube's caption
endpoint, so it can break or be rate-limited) and YouTube's official oEmbed endpoint for the title."""

from __future__ import annotations

import re
from typing import Any

import httpx

ID_RE = re.compile(r"(?:v=|/shorts/|/embed/|/live/|youtu\.be/|/v/)([A-Za-z0-9_-]{11})")


class NoTranscript(RuntimeError):
    pass


def video_id(url: str) -> str | None:
    url = url.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    m = ID_RE.search(url)
    return m.group(1) if m else None


def title(vid: str) -> dict[str, str]:
    try:
        r = httpx.get("https://www.youtube.com/oembed",
                      params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"}, timeout=15)
        if r.status_code == 200:
            d = r.json()
            return {"title": str(d.get("title") or ""), "author": str(d.get("author_name") or "")}
    except Exception:  # noqa: BLE001
        pass
    return {"title": "", "author": ""}


def _ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def transcript(vid: str) -> dict[str, Any]:
    """English captions (human-made preferred, auto-generated accepted), grouped into ~30 s timestamped lines."""
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    try:
        listing = api.list(vid)
        tracks = list(listing)
    except Exception as e:  # noqa: BLE001
        raise NoTranscript(f"YouTube returned no captions for this video ({type(e).__name__}).") from e
    english = [t for t in tracks if t.language_code.lower().startswith("en")]
    pick = next((t for t in english if not t.is_generated), None) or next(iter(english), None)
    if pick is None:
        langs = ", ".join(sorted({t.language_code for t in tracks})[:8]) or "none"
        raise NoTranscript(f"No English captions on this video (available: {langs}). Paste a transcript instead.")
    try:
        fetched = pick.fetch()
    except Exception as e:  # noqa: BLE001
        raise NoTranscript(f"Could not download the captions ({type(e).__name__}).") from e
    lines: list[str] = []
    block_start, block = None, []
    for sn in fetched.snippets:
        text = re.sub(r"\s+", " ", sn.text.replace("\n", " ")).strip()
        if not text or text in ("[Music]", "[Applause]"):
            continue
        if block_start is None:
            block_start = sn.start
        block.append(text)
        if sn.start - block_start >= 30:
            lines.append(f"[{_ts(block_start)}] {' '.join(block)}")
            block_start, block = None, []
    if block:
        lines.append(f"[{_ts(block_start or 0)}] {' '.join(block)}")
    if not lines:
        raise NoTranscript("The captions were empty.")
    return {"lines": lines, "auto_generated": pick.is_generated, "language": pick.language_code}
