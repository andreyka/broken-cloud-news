"""Shared story identity helpers used across selection and persistence."""

from __future__ import annotations

from collections import Counter
import re
from typing import Any

from bcn.briefing.text import canonical_url_key

_ISSUE_ID_RE = re.compile(
    r"\b(?:cve-\d{4}-\d+|ghsa-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4})\b"
)
_TOPIC_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "from",
        "with",
        "into",
        "over",
        "after",
        "before",
        "about",
        "this",
        "that",
        "onto",
        "cloud",
        "security",
        "vulnerability",
        "vulnerabilities",
        "issue",
        "issues",
        "patch",
        "patches",
        "advisory",
        "advisories",
        "exploit",
        "exploits",
        "fix",
        "fixes",
        "update",
        "updates",
        "attack",
        "attacks",
        "remote",
        "code",
        "execution",
        "allow",
        "allows",
        "new",
        "latest",
        "reported",
        "report",
        "today",
        "guide",
        "analysis",
        "risk",
        "risks",
        "zero",
        "wild",
        "active",
        "actively",
        "campaign",
        "campaigns",
        "threat",
        "threats",
    }
)


def normalize_story_title(title: str) -> str:
    """Normalize titles before duplicate matching."""
    normalized = (title or "").lower().strip()
    normalized = re.sub(r"https?://\S+", "", normalized)
    normalized = re.sub(r"[^a-z0-9\s\-_/.:]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def topic_signature(normalized_text: str) -> str:
    """Build a coarse topic signature for non-CVE/GHSA stories."""
    tokens = re.findall(r"[a-z0-9]{3,}", normalized_text)
    filtered = [
        tok for tok in tokens if tok not in _TOPIC_STOPWORDS and not tok.isdigit()
    ]
    if len(filtered) < 2:
        return ""

    counts = Counter(filtered)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    top_tokens = sorted(tok for tok, _ in ranked[:3])
    if len(top_tokens) < 2:
        return ""
    return "topic:" + "+".join(top_tokens)


def explicit_story_issue_keys_from_text(text: str) -> set[str]:
    """Extract only explicit advisory identifiers from text."""
    normalized = normalize_story_title(text)
    if not normalized:
        return set()
    return {m.group(0).lower() for m in _ISSUE_ID_RE.finditer(normalized)}


def story_issue_keys_from_text(text: str) -> set[str]:
    """Extract structured issue keys from normalized title/summary text."""
    normalized = normalize_story_title(text)
    if not normalized:
        return set()

    keys = {m.group(0).lower() for m in _ISSUE_ID_RE.finditer(normalized)}
    signature = topic_signature(normalized)
    if signature:
        keys.add(signature)
    return keys


def explicit_story_issue_keys(item: dict[str, Any] | None) -> set[str]:
    """Extract only explicit advisory identifiers from an item-like mapping."""
    payload = item or {}
    return explicit_story_issue_keys_from_text(
        f"{payload.get('title', '')} {payload.get('summary', '')}"
    )


def story_issue_keys(item: dict[str, Any] | None) -> set[str]:
    """Extract issue keys from an item-like mapping."""
    payload = item or {}
    return story_issue_keys_from_text(
        f"{payload.get('title', '')} {payload.get('summary', '')}"
    )


def story_url_key(url: str) -> str:
    """Return the canonical URL key used for story-level dedupe."""
    return canonical_url_key(url or "")


def primary_story_issue_key(title: str, summary: str) -> str:
    """Choose one strong issue key for DB-level grouping."""
    keys = explicit_story_issue_keys_from_text(f"{title} {summary}")
    if not keys:
        return ""

    return sorted(keys)[0]
