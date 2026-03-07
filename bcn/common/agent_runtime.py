"""Reusable agent transport and in-process execution helpers."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import httpx

from bcn.common.config import Settings

logger = logging.getLogger("bcn")


def extract_text_parts(parts: Any) -> str | None:
    """Extract non-empty text fragments from A2A `parts` payloads."""
    if not isinstance(parts, list):
        return None
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text)
            continue
        root = part.get("root")
        if not isinstance(root, dict):
            continue
        root_text = root.get("text")
        if isinstance(root_text, str) and root_text.strip():
            texts.append(root_text)
    if not texts:
        return None
    return "\n".join(texts)


def extract_text_from_rpc_result(result: dict[str, Any]) -> str | None:
    """Return agent text from known JSON-RPC response shapes."""
    payload = result.get("result")
    if not isinstance(payload, dict):
        return None

    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            text = extract_text_parts(artifact.get("parts"))
            if text:
                return text

    text = extract_text_parts(payload.get("parts"))
    if text:
        return text

    message = payload.get("message")
    if isinstance(message, dict):
        text = extract_text_parts(message.get("parts"))
        if text:
            return text

    status = payload.get("status")
    if isinstance(status, dict):
        text = extract_text_parts(status.get("parts"))
        if text:
            return text
        msg = status.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg
    return None


async def send_to_agent(
    target: str | int,
    skill: str,
    *,
    timeout_seconds: int = 180,
) -> str:
    """Send a JSON-RPC message to an A2A agent and return its reply."""
    from a2a.client import A2AClient
    from a2a.types import Message
    from a2a.types import MessageSendParams
    from a2a.types import SendMessageRequest
    from a2a.types import TextPart

    async with httpx.AsyncClient(timeout=timeout_seconds) as http_client:
        if isinstance(target, int):
            url = f"http://localhost:{target}"
        else:
            url = str(target or "").strip()
        client = A2AClient(http_client, url=url)

        message = Message(
            role="user",
            parts=[TextPart(text=skill)],
            message_id=uuid4().hex,
        )
        request = SendMessageRequest(
            id=uuid4().hex,
            params=MessageSendParams(message=message),
        )
        response = await client.send_message(request)

        result = response.model_dump(mode="json", exclude_none=True)
        text = extract_text_from_rpc_result(result)
        return text if text else str(result)


async def run_agent_directly(
    executor_cls: type,
    settings: Settings,
    skill: str,
) -> str:
    """Run an agent executor directly without the A2A server."""
    from a2a.server.agent_execution import RequestContext
    from a2a.types import Message
    from a2a.types import MessageSendParams
    from a2a.types import TextPart

    from bcn.common.db import close_pool
    from bcn.common.db import get_pool

    await get_pool(settings)
    executor = executor_cls(settings)

    class ResultCapture:
        """Lightweight event-queue stand-in that captures agent text output."""

        def __init__(self) -> None:
            self.messages: list[str] = []
            self._events: list[Any] = []

        @staticmethod
        def _extract_part_text(part: Any) -> str:
            text = getattr(part, "text", None)
            if isinstance(text, str):
                return text
            root = getattr(part, "root", None)
            root_text = getattr(root, "text", None) if root is not None else None
            return root_text if isinstance(root_text, str) else ""

        def enqueue_event(self, event: Any) -> None:
            self._events.append(event)
            try:
                parts = event.parts if hasattr(event, "parts") else []
                for part in parts:
                    text = self._extract_part_text(part).strip()
                    if text:
                        self.messages.append(text)
            except Exception:
                pass

    capture = ResultCapture()

    message = Message(
        role="user",
        parts=[TextPart(text=skill)],
        message_id=uuid4().hex,
    )
    params = MessageSendParams(message=message)
    context = RequestContext(request=params)

    try:
        await executor.execute(context=context, event_queue=capture)
        return "\n".join(capture.messages) if capture.messages else "Done"
    finally:
        try:
            close_fn = getattr(executor, "close", None)
            if callable(close_fn):
                maybe = close_fn()
                if hasattr(maybe, "__await__"):
                    await maybe
        except Exception:
            logger.exception("Failed to close %s executor", executor_cls.__name__)
        finally:
            await close_pool()
