"""Helpers for importing historical channel posts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import re
from zoneinfo import ZoneInfo

from bcn.briefing.text import canonical_url_key
from bcn.briefing.text import extract_raw_urls
from bcn.briefing.text import normalize_url

_HEADER_RE = re.compile(
    r"^(?P<author>.+?),\s+\[(?P<ts>\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}\s+(?:AM|PM))\]\s*$"
)


@dataclass(slots=True, frozen=True)
class ChannelHistoryPost:
    """One post parsed from a chat export."""

    author: str
    posted_at: datetime
    content_markdown: str

    @property
    def content_hash(self) -> str:
        """Stable hash used to make imports idempotent."""
        normalized = self.content_markdown.strip()
        token = f"{self.author}|{self.posted_at.isoformat()}|{normalized}"
        return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_channel_history_text(
    raw_text: str,
    *,
    timezone_name: str,
) -> list[ChannelHistoryPost]:
    """Parse Telegram-style `Name, [M/D/YYYY H:MM AM]` exports."""
    tz = ZoneInfo(timezone_name)
    lines = (raw_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    posts: list[ChannelHistoryPost] = []

    author: str | None = None
    posted_at: datetime | None = None
    body_lines: list[str] = []

    def flush_current() -> None:
        nonlocal author
        nonlocal posted_at
        nonlocal body_lines

        if author is None or posted_at is None:
            return
        content = "\n".join(body_lines).strip()
        if content:
            posts.append(
                ChannelHistoryPost(
                    author=author.strip(),
                    posted_at=posted_at,
                    content_markdown=content,
                )
            )
        author = None
        posted_at = None
        body_lines = []

    for raw_line in lines:
        line = raw_line.strip()
        match = _HEADER_RE.match(line)
        if match:
            flush_current()
            stamp = datetime.strptime(match.group("ts"), "%m/%d/%Y %I:%M %p")
            author = match.group("author")
            posted_at = stamp.replace(tzinfo=tz)
            continue

        if author is not None:
            body_lines.append(raw_line)

    flush_current()
    return posts


def extract_unique_post_urls(content_markdown: str) -> list[str]:
    """Extract canonicalized, first-seen URLs from a post body."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in extract_raw_urls(content_markdown or ""):
        key = canonical_url_key(raw) or normalize_url(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out
