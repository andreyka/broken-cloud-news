"""Shared helpers for workflow modes."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Awaitable
from collections.abc import Callable
import json
import logging
import re
from uuid import UUID

from bcn.workflows.distribution import execute_distribution
from bcn.workflows.runtime import require_runtime

logger = logging.getLogger(__name__)
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


async def run_writer_distributor_handoff(
    *,
    mode: str,
    run_generation: Callable[[str], Awaitable[WriterHandoffResult | str]],
    run_distribution: Callable[[str, UUID], Awaitable[str]],
) -> tuple[str, str | None]:
    """Run writer->distributor handoff from explicit writer payload."""
    writer_result = await run_generation(mode)
    if isinstance(writer_result, WriterHandoffResult):
        handoff = writer_result.handoff
        rendered_writer_result = render_writer_handoff_message(
            handoff,
            human_message=writer_result.human_message,
        )
    else:
        rendered_writer_result = str(writer_result)
        handoff = parse_writer_handoff_payload(rendered_writer_result)
    if not handoff:
        logger.warning(
            "Writer did not return structured handoff; skipping distribution. mode=%s writer_result=%s",
            mode,
            rendered_writer_result,
        )
        return rendered_writer_result, None

    if handoff.decision != "publish":
        logger.info(
            "Writer decision=%s; skipping distribution. mode=%s",
            handoff.decision,
            handoff.mode,
        )
        return rendered_writer_result, None

    if handoff.briefing_id is None:
        logger.warning(
            "Writer publish decision missing briefing_id; skipping distribution. mode=%s writer_result=%s",
            handoff.mode,
            rendered_writer_result,
        )
        return rendered_writer_result, None

    dispatch_mode = mode
    if handoff.mode and handoff.mode != mode:
        logger.warning(
            "Writer handoff mode mismatch (handoff=%s, expected=%s); using expected mode.",
            handoff.mode,
            mode,
        )
    distributor_result = await run_distribution(dispatch_mode, handoff.briefing_id)
    return rendered_writer_result, distributor_result


async def run_generation_and_distribution(mode: str) -> None:
    """Run one writer->distributor handoff cycle for the given workflow mode."""
    from bcn.workflows.generation import execute_generation_result

    settings, agent_client = require_runtime()
    await run_writer_distributor_handoff(
        mode=mode,
        run_generation=lambda workflow_mode: execute_generation_result(
            settings,
            mode=workflow_mode,
            source="scheduler",
            manage_pool=False,
        ),
        run_distribution=lambda dispatch_mode, briefing_id: execute_distribution(
            settings,
            mode=dispatch_mode,
            briefing_id=briefing_id,
            manage_pool=False,
        ),
    )
