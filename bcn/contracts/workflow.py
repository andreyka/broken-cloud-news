"""Typed workflow contracts and wire-format helpers."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from uuid import UUID

_WRITER_HANDOFF_PREFIX = "writer_handoff::"
_UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}\b"
)


@dataclass(frozen=True)
class WriterHandoff:
    """Structured writer->distributor orchestration payload."""

    mode: str
    decision: str
    briefing_id: UUID | None = None
    item_count: int | None = None


@dataclass(frozen=True)
class WriterHandoffResult:
    """Typed internal handoff result with optional human-readable context."""

    handoff: WriterHandoff
    human_message: str = ""


def extract_briefing_id(text: str) -> UUID | None:
    """Extract a briefing UUID from writer/distributor message text."""
    match = _UUID_PATTERN.search(text or "")
    if not match:
        return None
    try:
        return UUID(match.group(0))
    except ValueError:
        return None


def render_writer_handoff_payload(
    *,
    mode: str,
    decision: str,
    briefing_id: UUID | None = None,
    item_count: int | None = None,
) -> str:
    """Render writer handoff as an explicit, machine-readable contract."""
    payload: dict[str, object] = {
        "mode": str(mode or "").strip().lower(),
        "decision": str(decision or "").strip().lower(),
    }
    if briefing_id is not None:
        payload["briefing_id"] = str(briefing_id)
    if item_count is not None:
        payload["item_count"] = max(0, int(item_count))
    return _WRITER_HANDOFF_PREFIX + json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def render_writer_handoff_message(
    handoff: WriterHandoff,
    *,
    human_message: str = "",
) -> str:
    """Render a typed handoff plus optional human-readable text."""
    payload = render_writer_handoff_payload(
        mode=handoff.mode,
        decision=handoff.decision,
        briefing_id=handoff.briefing_id,
        item_count=handoff.item_count,
    )
    text = str(human_message or "").strip()
    return payload if not text else f"{payload}\n{text}"


def parse_writer_handoff_payload(text: str) -> WriterHandoff | None:
    """Parse the structured writer handoff payload from response text."""
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith(_WRITER_HANDOFF_PREFIX):
            continue

        raw_payload = line[len(_WRITER_HANDOFF_PREFIX) :].strip()
        if not raw_payload:
            continue

        try:
            decoded = json.loads(raw_payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict):
            continue

        mode = str(decoded.get("mode", "") or "").strip().lower()
        decision = str(decoded.get("decision", "") or "").strip().lower()
        if not mode or not decision:
            continue

        briefing_id: UUID | None = None
        raw_briefing_id = str(decoded.get("briefing_id", "") or "").strip()
        if raw_briefing_id:
            try:
                briefing_id = UUID(raw_briefing_id)
            except ValueError:
                continue

        item_count: int | None = None
        if "item_count" in decoded and decoded.get("item_count") is not None:
            try:
                item_count = max(0, int(decoded.get("item_count")))
            except (TypeError, ValueError):
                continue

        return WriterHandoff(
            mode=mode,
            decision=decision,
            briefing_id=briefing_id,
            item_count=item_count,
        )
    return None


__all__ = [
    "WriterHandoff",
    "WriterHandoffResult",
    "extract_briefing_id",
    "parse_writer_handoff_payload",
    "render_writer_handoff_message",
    "render_writer_handoff_payload",
]
