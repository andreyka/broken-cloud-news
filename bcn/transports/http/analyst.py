"""HTTP client for remote analyst service deployments."""

from __future__ import annotations

from typing import Any

from bcn.common.models import AnalyzedItemUpdate
from bcn.contracts.analyst import AnalystItemRequest
from bcn.contracts.analyst import analyzed_item_from_payload
from bcn.transports.http._json_client import JsonHttpServiceClient
from bcn.transports.http.routes import ANALYST_ANALYZE_ITEM_PATH


class RemoteAnalystClient(JsonHttpServiceClient):
    """Remote analyst client over JSON/HTTP."""

    async def analyze_item(self, item: dict[str, Any]) -> AnalyzedItemUpdate:
        """Analyze one collected item via the remote analyst service."""
        request = AnalystItemRequest(item=dict(item))
        payload = await self._post_json(
            ANALYST_ANALYZE_ITEM_PATH,
            request.to_payload(),
        )
        return analyzed_item_from_payload(payload)
