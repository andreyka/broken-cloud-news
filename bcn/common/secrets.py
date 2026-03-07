"""Secret redaction helpers for logs and persisted metadata."""

from __future__ import annotations

from collections.abc import Iterable
import re
from typing import Any

_REDACTED = "[REDACTED]"
_TELEGRAM_BOT_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_SLACK_WEBHOOK_RE = re.compile(
    r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)(authorization:\s*(?:bot|bearer)\s+)([A-Za-z0-9._:-]+)"
)
_BOT_PATH_RE = re.compile(r"/bot(\d{6,12}:[A-Za-z0-9_-]{20,})")


def redact_sensitive_text(
    value: object,
    *,
    secrets: Iterable[str] | None = None,
) -> str:
    """Return a string with known secrets and token-shaped substrings redacted."""
    text = str(value or "")
    if not text:
        return ""

    redacted = text
    for secret in secrets or ():
        candidate = str(secret or "").strip()
        if candidate:
            redacted = redacted.replace(candidate, _REDACTED)

    redacted = _TELEGRAM_BOT_TOKEN_RE.sub(_REDACTED, redacted)
    redacted = _SLACK_WEBHOOK_RE.sub(_REDACTED, redacted)
    redacted = _AUTH_HEADER_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _BOT_PATH_RE.sub(f"/bot{_REDACTED}", redacted)
    return redacted


def redact_error_text(
    exc: BaseException,
    *,
    secrets: Iterable[str] | None = None,
) -> str:
    """Return a safe one-line exception string suitable for logs."""
    text = redact_sensitive_text(str(exc), secrets=secrets).strip()
    if text:
        return text
    return exc.__class__.__name__


def redact_sensitive_value(
    value: Any,
    *,
    secrets: Iterable[str] | None = None,
) -> Any:
    """Recursively redact strings in nested JSON-like payloads."""
    if isinstance(value, dict):
        return {
            str(key): redact_sensitive_value(inner, secrets=secrets)
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_value(inner, secrets=secrets) for inner in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_value(inner, secrets=secrets) for inner in value)
    if isinstance(value, str):
        return redact_sensitive_text(value, secrets=secrets)
    return value
