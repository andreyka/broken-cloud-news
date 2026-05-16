"""Text normalization helpers for briefing generation and fallback formatting."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlparse

_TEMPLATE_HEADING_PREFIX = re.compile(
    r"^\*\*(detection|source|threat|response|mitigation|intel)\s*:\s*(.+?)\*\*$",
    flags=re.IGNORECASE,
)
_SOURCE_FIELD_LINE = re.compile(
    r"^\*?\s*source\s*:\s*(.+?)\s*\*?$", flags=re.IGNORECASE
)
_TRACKING_PARAM_NAMES = frozenset(
    {
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "mkt_tok",
        "msclkid",
        "rb_clickid",
        "s_cid",
        "vero_conv",
        "vero_id",
        "yclid",
    }
)
_URL_PATTERN = re.compile(r"https?://[^\s)\]>]+")
_BULLET_SECTION_BLOCK = re.compile(
    r"^\s*[-*]\s+\*\*(.+?)\*\*\s*[—:-]\s*(.+\S)\s*$",
    flags=re.DOTALL,
)
_SOCIAL_WRAPPER_HOSTS = frozenset({"t.co", "x.com", "twitter.com", "mobile.twitter.com"})


def normalize_url(url: str) -> str:
    """Normalize URLs for robust inclusion checks."""
    trimmed = url.strip().rstrip(").,;!?")
    try:
        parsed = urlparse(trimmed)
    except Exception:
        return trimmed
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path.rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{netloc}{path}{query}"


def extract_raw_urls(markdown: str) -> list[str]:
    """Extract raw HTTP(S) URLs from markdown/plain text."""
    return _URL_PATTERN.findall(markdown or "")


def extract_urls_in_order(markdown: str) -> list[str]:
    """Extract raw HTTP(S) URLs preserving first-seen order."""
    return list(dict.fromkeys(extract_raw_urls(markdown)))


def extract_urls(markdown: str) -> set[str]:
    """Extract normalized HTTP(S) URLs from markdown/plain text."""
    raw_urls = extract_raw_urls(markdown)
    return {normalize_url(u) for u in raw_urls if u}


def extract_url_keys(markdown: str) -> set[str]:
    """Extract canonical URL keys for robust URL inclusion checks."""
    raw_urls = extract_raw_urls(markdown)
    return {
        key for key in (canonical_url_key(u) for u in raw_urls if u) if key
    }


def sanitize_item_title(title: str) -> str:
    """Strip embedded URLs and low-signal wrapper noise from item titles."""
    original = str(title or "").strip()
    if not original:
        return ""
    cleaned = _URL_PATTERN.sub("", original)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—:;,|")
    return cleaned or original


def dedupe_markdown_links(markdown: str) -> str:
    """Remove duplicate markdown links while keeping readable labels."""
    seen: set[str] = set()

    def repl(match: re.Match[str]) -> str:
        label = match.group(1)
        url = match.group(2)
        key = canonical_url_key(url)
        if key in seen:
            return label
        seen.add(key)
        return match.group(0)

    return re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", repl, markdown)


def canonical_url_key(url: str) -> str:
    """Build canonical URL key for dedupe by stripping tracking variants."""
    normalized = normalize_url(url)
    if not normalized:
        return ""
    try:
        parsed = urlparse(normalized)
    except Exception:
        return normalized

    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path.rstrip("/")

    query_params: list[tuple[str, str]] = []
    for raw_key, raw_value in parse_qsl(parsed.query, keep_blank_values=True):
        key = raw_key.lower()
        if (
            key.startswith("utm_")
            or key.startswith("mc_")
            or key in _TRACKING_PARAM_NAMES
        ):
            continue
        query_params.append((key, raw_value))
    query_params.sort()

    query = urlencode(query_params, doseq=True)
    if query:
        return f"{scheme}://{netloc}{path}?{query}"
    return f"{scheme}://{netloc}{path}"


def is_social_wrapper_url(url: str) -> bool:
    """Return whether a URL is one social wrapper that should not be preferred."""
    normalized = normalize_url(url)
    if not normalized:
        return False
    try:
        host = urlparse(normalized).netloc.lower()
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    return host in _SOCIAL_WRAPPER_HOSTS


def item_url_candidates(item: dict) -> list[str]:
    """Return canonical candidate URLs that can satisfy one selected item."""
    raw = to_dict(item.get("raw_data"))
    seen: set[str] = set()
    candidates: list[str] = []

    def _add(candidate: object) -> None:
        url = str(candidate or "").strip()
        if not url.startswith(("http://", "https://")):
            return
        key = canonical_url_key(url)
        if not key or key in seen:
            return
        seen.add(key)
        candidates.append(url)

    for field in ("url", "expanded_url", "unwound_url", "canonical_url"):
        _add(item.get(field))

    for ref in raw.get("references", []) if isinstance(raw.get("references"), list) else []:
        if isinstance(ref, dict):
            _add(ref.get("url"))
        else:
            _add(ref)

    entities = raw.get("entities", {}) if isinstance(raw, dict) else {}
    urls = entities.get("urls", []) if isinstance(entities, dict) else []
    for entry in urls if isinstance(urls, list) else []:
        if isinstance(entry, dict):
            for field in ("unwound_url", "expanded_url", "url"):
                _add(entry.get(field))
        else:
            _add(entry)

    return candidates


def item_url_keys(item: dict) -> set[str]:
    """Return canonical URL keys that should count as coverage for one item."""
    return {
        key
        for key in (canonical_url_key(url) for url in item_url_candidates(item))
        if key
    }


def preferred_item_url(item: dict) -> str:
    """Return the best public URL to show for one selected item."""
    candidates = item_url_candidates(item)
    for candidate in candidates:
        if not is_social_wrapper_url(candidate):
            return candidate
    return candidates[0] if candidates else ""


def missing_items_for_markdown(markdown: str, items: list[dict]) -> list[dict]:
    """Return selected items whose main URLs are missing from markdown."""
    present_keys = extract_url_keys(markdown)
    missing: list[dict] = []
    for item in items:
        expected_keys = item_url_keys(item)
        if expected_keys and present_keys.isdisjoint(expected_keys):
            missing.append(item)
    return missing


def append_missing_items_section(markdown: str, missing_items: list[dict]) -> str:
    """Append missing item references in a readable non-templated layout."""
    if not missing_items:
        return markdown

    entries: list[str] = ["🔗 **References:**"]
    for item in missing_items:
        title = sanitize_item_title(str(item.get("title") or "Untitled item"))
        url = preferred_item_url(item)
        if not url:
            continue
        entries.append(f"• [{title}]({url})")

    if len(entries) == 1:
        return markdown

    suffix = "\n".join(entries)
    return markdown.rstrip() + "\n\n" + suffix


def clip_markdown(markdown: str, limit: int) -> str:
    """Hard-cap markdown length, preferring paragraph boundaries."""
    if len(markdown) <= limit:
        return markdown
    split_at = markdown.rfind("\n\n", 0, limit)
    if split_at == -1:
        split_at = markdown.rfind("\n", 0, limit)
    if split_at == -1:
        split_at = limit
    return markdown[:split_at].rstrip()


def normalize_section_headings(markdown: str) -> str:
    """Convert markdown headings to Telegram-friendly bold section lines."""
    body = (markdown or "").strip()
    if body and not re.search(r"(?m)^\*\*.+\*\*\s*$", body):
        blocks = [block.strip() for block in re.split(r"\n\s*\n", body) if block.strip()]
        converted: list[str] = []
        converted_count = 0
        for block in blocks:
            match = _BULLET_SECTION_BLOCK.match(block)
            if not match:
                converted.append(block)
                continue
            converted_count += 1
            title = match.group(1).strip()
            content = match.group(2).strip()
            converted.append(f"**{title}**\n{content}")
        if converted_count >= 2:
            body = "\n\n".join(converted)

    lines = body.splitlines()
    normalized: list[str] = []
    for line in lines:
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if not match:
            normalized.append(line)
            continue
        heading = match.group(1).strip()
        heading = re.sub(r"^\*\*(.+)\*\*$", r"\1", heading).strip()
        normalized.append(f"**{heading}**")
    return "\n".join(normalized)


def de_template_fields(markdown: str) -> str:
    """Remove repetitive `Detection:`/`Source:` field-style scaffolding."""
    lines = markdown.splitlines()
    out: list[str] = []

    for line in lines:
        stripped = line.strip()
        heading_match = _TEMPLATE_HEADING_PREFIX.match(stripped)
        if heading_match:
            line = f"**{heading_match.group(2).strip()}**"
            stripped = line.strip()

        source_match = _SOURCE_FIELD_LINE.match(stripped)
        if source_match:
            reference = source_match.group(1).strip()
            idx = len(out) - 1
            while idx >= 0:
                candidate = out[idx].strip()
                if (
                    not candidate
                    or candidate == "---"
                    or re.fullmatch(r"\*\*.+\*\*", candidate)
                ):
                    idx -= 1
                    continue
                break

            if idx >= 0:
                if "reference:" not in out[idx].lower():
                    out[idx] = out[idx].rstrip() + f" (reference: {reference})"
            else:
                out.append(f"Reference: {reference}")
            continue

        out.append(line)

    return "\n".join(out)


def to_dict(raw_data: object) -> dict:
    """Safely coerce DB raw_data payload to dict."""
    if isinstance(raw_data, dict):
        return raw_data
    if isinstance(raw_data, str):
        try:
            parsed = json.loads(raw_data)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
