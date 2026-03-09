"""Typed distributor contracts shared by workflows and transport adapters."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
import json
from typing import Any

REGULAR_DAILY_BRIEFING_MODE = "regular_daily_briefing"
REGULAR_MONTHLY_NEWSLETTER_MODE = "regular_monthly_newsletter"
_SUPPORTED_MODES = frozenset(
    (
        REGULAR_DAILY_BRIEFING_MODE,
        "ad_hoc",
        REGULAR_MONTHLY_NEWSLETTER_MODE,
    )
)
_DELIVERY_REQUEST_PREFIX = "deliver_briefing::"
_DELIVERY_RESULT_PREFIX = "delivery_result::"


@dataclass(frozen=True)
class ChannelDeliveryResult:
    """Structured outcome for one channel send attempt."""

    channel: str
    status: str
    external_message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryRequest:
    """Explicit distributor worker input prepared by the control plane."""

    briefing: dict[str, Any]
    mode: str
    newsletter_recipients: tuple[str, ...] = ()
    previous_ok_channels: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DeliveryResult:
    """Structured distributor worker output consumed by the control plane."""

    mode: str
    results: dict[str, str]
    attempts: tuple[ChannelDeliveryResult, ...]
    all_ok: bool
    message: str


def normalize_distribution_mode(mode: str | None) -> str:
    """Return a supported distribution mode or the daily default."""
    normalized = str(mode or "").strip().lower()
    if normalized in _SUPPORTED_MODES:
        return normalized
    return REGULAR_DAILY_BRIEFING_MODE


def _json_safe_payload(value: Any) -> Any:
    """Convert nested transport payloads into JSON-safe primitives."""
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
    )


def _coerce_briefing_datetimes(briefing: dict[str, Any]) -> dict[str, Any]:
    """Parse known datetime fields back into native values when possible."""
    parsed = dict(briefing)
    raw_created_at = parsed.get("created_at")
    if isinstance(raw_created_at, str) and raw_created_at.strip():
        try:
            parsed["created_at"] = datetime.fromisoformat(
                raw_created_at.replace("Z", "+00:00")
            )
        except ValueError:
            pass
    return parsed


def delivery_request_to_payload(request: DeliveryRequest) -> dict[str, Any]:
    """Render one delivery request as a JSON-safe transport payload."""
    return {
        "briefing": _json_safe_payload(dict(request.briefing)),
        "mode": normalize_distribution_mode(request.mode),
        "newsletter_recipients": list(request.newsletter_recipients),
        "previous_ok_channels": sorted(request.previous_ok_channels),
    }


def delivery_request_from_payload(payload: dict[str, Any] | None) -> DeliveryRequest | None:
    """Build one delivery request from a JSON payload."""
    decoded = payload or {}
    if not isinstance(decoded, dict):
        return None

    raw_briefing = decoded.get("briefing")
    if not isinstance(raw_briefing, dict):
        return None

    recipients = tuple(
        str(value).strip()
        for value in decoded.get("newsletter_recipients", [])
        if str(value).strip()
    )
    previous_ok_channels = frozenset(
        str(value).strip().lower()
        for value in decoded.get("previous_ok_channels", [])
        if str(value).strip()
    )
    return DeliveryRequest(
        briefing=_coerce_briefing_datetimes(raw_briefing),
        mode=normalize_distribution_mode(decoded.get("mode")),
        newsletter_recipients=recipients,
        previous_ok_channels=previous_ok_channels,
    )


def delivery_result_to_payload(result: DeliveryResult) -> dict[str, Any]:
    """Render one delivery result as a JSON-safe transport payload."""
    return {
        "all_ok": bool(result.all_ok),
        "attempts": [
            {
                "channel": attempt.channel,
                "external_message_id": attempt.external_message_id,
                "metadata": _json_safe_payload(attempt.metadata),
                "status": attempt.status,
            }
            for attempt in result.attempts
        ],
        "message": result.message,
        "mode": normalize_distribution_mode(result.mode),
        "results": _json_safe_payload(dict(result.results)),
    }


def delivery_result_from_payload(payload: dict[str, Any] | None) -> DeliveryResult | None:
    """Build one delivery result from a JSON payload."""
    decoded = payload or {}
    if not isinstance(decoded, dict):
        return None

    results_raw = decoded.get("results")
    if not isinstance(results_raw, dict):
        return None
    results = {
        str(channel).strip().lower(): str(status).strip().lower()
        for channel, status in results_raw.items()
        if str(channel).strip()
    }

    attempts_raw = decoded.get("attempts", [])
    if not isinstance(attempts_raw, list):
        return None
    attempts: list[ChannelDeliveryResult] = []
    for item in attempts_raw:
        if not isinstance(item, dict):
            continue
        channel = str(item.get("channel") or "").strip().lower()
        status = str(item.get("status") or "").strip().lower()
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        if not channel or not status:
            continue
        attempts.append(
            ChannelDeliveryResult(
                channel=channel,
                status=status,
                external_message_id=(
                    str(item.get("external_message_id")).strip()
                    if item.get("external_message_id") is not None
                    else None
                ),
                metadata=dict(metadata),
            )
        )

    return DeliveryResult(
        mode=normalize_distribution_mode(decoded.get("mode")),
        results=results,
        attempts=tuple(attempts),
        all_ok=bool(decoded.get("all_ok")),
        message=str(decoded.get("message") or "").strip(),
    )


def render_delivery_request_payload(request: DeliveryRequest) -> str:
    """Render a structured delivery request for text-based transport adapters."""
    return _DELIVERY_REQUEST_PREFIX + json.dumps(
        delivery_request_to_payload(request),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def parse_delivery_request_payload(text: str) -> DeliveryRequest | None:
    """Parse a structured delivery request from transport input text."""
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith(_DELIVERY_REQUEST_PREFIX):
            continue
        raw_payload = line[len(_DELIVERY_REQUEST_PREFIX) :].strip()
        if not raw_payload:
            continue
        try:
            decoded = json.loads(raw_payload)
        except json.JSONDecodeError:
            continue
        request = delivery_request_from_payload(decoded)
        if request is not None:
            return request
    return None


def render_delivery_result_payload(result: DeliveryResult) -> str:
    """Render a structured delivery result for text-based transport adapters."""
    return _DELIVERY_RESULT_PREFIX + json.dumps(
        delivery_result_to_payload(result),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def parse_delivery_result_payload(text: str) -> DeliveryResult | None:
    """Parse a structured delivery result from transport output text."""
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith(_DELIVERY_RESULT_PREFIX):
            continue
        raw_payload = line[len(_DELIVERY_RESULT_PREFIX) :].strip()
        if not raw_payload:
            continue
        try:
            decoded = json.loads(raw_payload)
        except json.JSONDecodeError:
            continue
        result = delivery_result_from_payload(decoded)
        if result is not None:
            return result
    return None


__all__ = [
    "ChannelDeliveryResult",
    "DeliveryRequest",
    "DeliveryResult",
    "REGULAR_DAILY_BRIEFING_MODE",
    "REGULAR_MONTHLY_NEWSLETTER_MODE",
    "delivery_request_from_payload",
    "delivery_request_to_payload",
    "delivery_result_from_payload",
    "delivery_result_to_payload",
    "normalize_distribution_mode",
    "parse_delivery_request_payload",
    "parse_delivery_result_payload",
    "render_delivery_request_payload",
    "render_delivery_result_payload",
]
