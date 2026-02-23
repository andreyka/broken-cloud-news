"""Text normalization helpers for briefing generation and fallback formatting."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode, urlparse

_TEMPLATE_HEADING_PREFIX = re.compile(
    r"^\*\*(detection|source|threat|response|mitigation|intel)\s*:\s*(.+?)\*\*$",
    flags=re.IGNORECASE,
)
_SOURCE_FIELD_LINE = re.compile(r"^\*?\s*source\s*:\s*(.+?)\s*\*?$", flags=re.IGNORECASE)
_TRACKING_PARAM_NAMES = frozenset({
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
})


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


def extract_urls(markdown: str) -> set[str]:
    """Extract normalized HTTP(S) URLs from markdown/plain text."""
    raw_urls = re.findall(r"https?://[^\s)\]>]+", markdown)
    return {normalize_url(u) for u in raw_urls if u}


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
        if key.startswith("utm_") or key.startswith("mc_") or key in _TRACKING_PARAM_NAMES:
            continue
        query_params.append((key, raw_value))
    query_params.sort()

    query = urlencode(query_params, doseq=True)
    if query:
        return f"{scheme}://{netloc}{path}?{query}"
    return f"{scheme}://{netloc}{path}"


def missing_items_for_markdown(markdown: str, items: list[dict]) -> list[dict]:
    """Return selected items whose main URLs are missing from markdown."""
    present_urls = extract_urls(markdown)
    missing: list[dict] = []
    for item in items:
        url = normalize_url(str(item.get("url", "")))
        if url and url not in present_urls:
            missing.append(item)
    return missing


def append_missing_items_section(markdown: str, missing_items: list[dict]) -> str:
    """Append missing item references in a readable non-templated layout."""
    if not missing_items:
        return markdown

    entries: list[str] = []
    for item in missing_items:
        title = str(item.get("title") or "Untitled item").strip()
        summary = str(item.get("summary") or "").strip()
        summary = re.sub(r"\s+", " ", summary)
        if len(summary) > 180:
            summary = summary[:177].rstrip() + "..."
        url = str(item.get("url") or "").strip()
        if not url:
            continue

        if summary:
            entries.append(f"[{title}]({url}) — {summary}")
        else:
            entries.append(f"[{title}]({url})")

    if not entries:
        return markdown

    # Add visual spacing between entries for Telegram readability.
    suffix = "\n\n".join(entries)
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
    lines = markdown.splitlines()
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
                if not candidate or candidate == "---" or re.fullmatch(r"\*\*.+\*\*", candidate):
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
