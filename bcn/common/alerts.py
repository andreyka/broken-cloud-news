"""Operational alerts to the operator's private Telegram chat.

Distinct from the public distribution channel: alerts go to
BCN_ALERT_TELEGRAM_CHAT_ID (the operator's own chat), never to
BCN_TELEGRAM_CHAT_ID (the subscriber channel).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from bcn.common.config import Settings

logger = logging.getLogger(__name__)


async def send_operator_alert(settings: "Settings", message: str) -> bool:
    """Deliver an operational alert; logs instead when delivery is off. Never raises."""
    enabled = bool(getattr(settings, "alerts_enabled", False))
    token = str(getattr(settings, "telegram_bot_token", "") or "")
    chat_id = str(getattr(settings, "alert_telegram_chat_id", "") or "")
    if not enabled or not token or not chat_id:
        logger.error("OPERATOR ALERT (delivery disabled): %s", message)
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"⚠️ Broken Cloud alert\n{message}",
                },
            )
            response.raise_for_status()
        return True
    except Exception:
        logger.exception("Operator alert delivery failed: %s", message)
        return False


def quiet_streak_alert_due(streak: int, threshold: int) -> bool:
    """Alert when the streak reaches the threshold, then every third skip after."""
    if threshold <= 0 or streak < threshold:
        return False
    return (streak - threshold) % 3 == 0


async def consecutive_quiet_skips() -> int:
    """Count the most recent unbroken run of quiet-day skips."""
    from bcn.persistence.runtime import get_pool

    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT decision, coalesce(decision_reason, '') AS reason
        FROM generation_runs ORDER BY created_at DESC LIMIT 15
        """
    )
    streak = 0
    for row in rows:
        if str(row["decision"]) == "BLOCKED" and str(row["reason"]).startswith(
            "Quiet day"
        ):
            streak += 1
        else:
            break
    return streak
