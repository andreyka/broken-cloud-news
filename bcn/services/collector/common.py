"""Shared normalization helpers for collector source adapters."""

from __future__ import annotations

import calendar
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from email.utils import parsedate_to_datetime
import html
import logging
import re
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SKIP_SCRAPE_DOMAINS = frozenset(
    {
        "nvd.nist.gov",
        "cve.mitre.org",
        "cve.org",
        "access.redhat.com/errata",
    }
)
REDDIT_LOW_SIGNAL_DOMAINS = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "instagram.com",
        "facebook.com",
        "tiktok.com",
        "x.com",
        "twitter.com",
        "imgur.com",
        "giphy.com",
    }
)
REDDIT_TECHNICAL_DOMAIN_HINTS = frozenset(
    {
        "github.com",
        "gitlab.com",
        "cisa.gov",
        "nist.gov",
        "mitre.org",
        "aws.amazon.com",
        "cloud.google.com",
        "security.googleblog.com",
        "unit42.paloaltonetworks.com",
        "research.checkpoint.com",
        "stepsecurity.io",
    }
)
REDDIT_TECHNICAL_HINTS = (
    "advisory",
    "cve",
    "vuln",
    "exploit",
    "rce",
    "xss",
    "ssrf",
    "auth",
    "bypass",
    "privilege",
    "escape",
    "container",
    "kubernetes",
    "k8s",
    "cloud",
    "iam",
    "s3",
    "supply chain",
    "supply-chain",
    "zero-day",
    "zeroday",
    "writeup",
    "write-up",
    "research",
    "patch",
    "poc",
    "proof of concept",
    "github actions",
)
MAX_FUTURE_SOURCE_SKEW = timedelta(hours=6)


def is_cloud_security_relevant(settings: Any, text: str) -> bool:
    """Return whether the text looks relevant to cloud security."""
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    if not normalized or len(normalized) < 20:
        return False

    required = getattr(settings, "twitter_required_keywords", [])
    if required and not any(kw.lower() in normalized for kw in required):
        return False

    if "ctf" in normalized and not any(
        token in normalized for token in ("cloud", "k8s", "container", "cve", "vuln")
    ):
        return False
    if normalized.startswith("rt @"):
        return False

    return True


def clean_summary(value: str) -> str:
    """Strip basic HTML tags/entities from feed summaries."""
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def feed_entry_value(entry: Any, field: str) -> Any:
    """Read one feed field without feedparser's attribute alias fallback."""
    if hasattr(entry, "get"):
        return entry.get(field)
    return getattr(entry, field, None)


def coerce_feed_datetime(value: Any) -> datetime | None:
    """Normalize feed date values into aware UTC datetimes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                return None
    elif hasattr(value, "tm_year") and hasattr(value, "tm_mon"):
        dt = datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    else:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_source_timestamp(
    published_at: datetime | None,
    *,
    source_type: str,
    source_id: str,
    title: str,
    url: str,
    field: str,
) -> datetime | None:
    """Drop items with missing or implausible source timestamps."""
    label = title or url or source_id
    if published_at is None:
        logger.warning(
            "Dropping %s item without parseable timestamp: %s [field=%s]",
            source_type,
            label,
            field,
        )
        return None
    now = datetime.now(timezone.utc)
    if published_at > now + MAX_FUTURE_SOURCE_SKEW:
        logger.warning(
            "Dropping %s item with future timestamp: %s [field=%s published_at=%s]",
            source_type,
            label,
            field,
            published_at.isoformat(),
        )
        return None
    return published_at


def extract_feed_published_at(
    entry: Any,
) -> tuple[datetime | None, str | None, str]:
    """Return the best available feed timestamp for one entry."""
    for field in (
        "published_parsed",
        "updated_parsed",
        "created_parsed",
        "published",
        "updated",
        "created",
    ):
        raw_value = feed_entry_value(entry, field)
        published_at = coerce_feed_datetime(raw_value)
        if published_at is not None:
            raw_text = raw_value if isinstance(raw_value, str) else None
            return published_at, raw_text, field
    return None, None, "missing_or_unparseable"


def extract_reddit_post_id(source_id: str, url: str) -> str:
    """Extract the Reddit post id from a feed source id or permalink."""
    sid = (source_id or "").strip()
    if sid.startswith("t3_"):
        return sid[3:]

    match = re.search(r"/comments/([a-z0-9]+)/", url or "", flags=re.IGNORECASE)
    if match:
        return match.group(1).lower()
    return ""


def is_internal_twitter_url(url: str) -> bool:
    """Return whether a URL points back to X/Twitter itself."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    return host in {"x.com", "twitter.com", "mobile.twitter.com", "t.co"}


def extract_tweet_reference_urls(tweet: dict[str, Any]) -> list[str]:
    """Extract external reference URLs from tweet entities."""
    entities = tweet.get("entities", {}) if isinstance(tweet, dict) else {}
    urls = entities.get("urls", []) if isinstance(entities, dict) else []
    seen: set[str] = set()
    out: list[str] = []

    for item in urls:
        if isinstance(item, dict):
            candidates = [
                item.get("unwound_url", ""),
                item.get("expanded_url", ""),
                item.get("url", ""),
            ]
        else:
            candidates = [str(item)]

        for candidate in candidates:
            url = str(candidate or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            if is_internal_twitter_url(url):
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
            break

    return out


def extract_reddit_reference_urls(
    permalink: str,
    metadata: dict[str, Any],
) -> list[str]:
    """Extract non-Reddit outbound URLs from Reddit post metadata."""
    permalink_norm = (permalink or "").strip().rstrip("/")
    candidates = [
        metadata.get("url_overridden_by_dest", ""),
        metadata.get("url", ""),
    ]
    seen: set[str] = set()
    out: list[str] = []

    for candidate in candidates:
        url = str(candidate or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        if is_internal_reddit_url(url):
            continue
        if url.rstrip("/") == permalink_norm:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)

    return out


def select_reddit_primary_url(
    permalink: str,
    references: list[str],
    *,
    title: str = "",
    summary: str = "",
) -> str:
    """Prefer outbound source only when it looks technically useful."""
    for reference in references:
        if is_useful_reddit_reference(reference, title, summary):
            return reference
    return (permalink or "").strip()


def is_internal_reddit_url(url: str) -> bool:
    """Return whether a URL points to Reddit-owned domains."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    return host in {
        "reddit.com",
        "old.reddit.com",
        "new.reddit.com",
        "np.reddit.com",
        "redd.it",
        "i.redd.it",
        "v.redd.it",
        "redditmedia.com",
    }


def is_useful_reddit_reference(url: str, title: str, summary: str) -> bool:
    """Return whether an outbound Reddit link looks technically useful."""
    if not url.startswith(("http://", "https://")):
        return False
    if is_internal_reddit_url(url):
        return False

    try:
        parsed = urlparse(url)
    except Exception:
        return False

    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return False
    if host_matches(host, REDDIT_LOW_SIGNAL_DOMAINS):
        return False
    if host_matches(host, REDDIT_TECHNICAL_DOMAIN_HINTS):
        return True

    url_text = f"{host}{parsed.path} {parsed.query}".lower()
    context_text = f"{title} {summary}".lower()
    technical_hits = 0
    if any(hint in url_text for hint in REDDIT_TECHNICAL_HINTS):
        technical_hits += 1
    if any(hint in context_text for hint in REDDIT_TECHNICAL_HINTS):
        technical_hits += 1
    return technical_hits >= 2


def host_matches(host: str, domains: frozenset[str]) -> bool:
    """Return whether a host equals or is a subdomain of a listed domain."""
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def normalize_reddit_permalink(permalink: str) -> str:
    """Normalize Reddit relative permalinks to absolute URLs."""
    text = (permalink or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    if text.startswith("/"):
        return f"https://www.reddit.com{text}"
    return f"https://www.reddit.com/{text}"


def build_reddit_full_content(
    title: str,
    summary: str,
    references: list[str],
) -> str | None:
    """Compose analysis-friendly Reddit content with outbound references."""
    text = (summary or "").strip()
    if text.lower().startswith("submitted by "):
        text = ""
    if not text:
        text = (title or "").strip()
    if not references:
        return text or None

    refs_block = "\n".join(f"- {reference}" for reference in references[:6])
    if not text:
        return f"Reference links:\n{refs_block}"
    return f"{text}\n\nReference links:\n{refs_block}"


def build_tweet_full_content(
    tweet_text: str,
    references: list[str],
) -> str | None:
    """Compose analysis-friendly tweet content with extracted references."""
    text = (tweet_text or "").strip()
    if not references:
        return text or None

    refs_block = "\n".join(f"- {reference}" for reference in references[:6])
    if not text:
        return f"Reference links:\n{refs_block}"
    return f"{text}\n\nReference links:\n{refs_block}"


__all__ = [
    "SKIP_SCRAPE_DOMAINS",
    "build_reddit_full_content",
    "build_tweet_full_content",
    "clean_summary",
    "coerce_feed_datetime",
    "extract_feed_published_at",
    "extract_reddit_post_id",
    "extract_reddit_reference_urls",
    "extract_tweet_reference_urls",
    "feed_entry_value",
    "is_cloud_security_relevant",
    "is_internal_reddit_url",
    "is_internal_twitter_url",
    "is_useful_reddit_reference",
    "normalize_reddit_permalink",
    "select_reddit_primary_url",
    "validate_source_timestamp",
]
