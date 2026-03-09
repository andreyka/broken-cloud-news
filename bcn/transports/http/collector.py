"""HTTP client for remote collector service deployments."""

from __future__ import annotations

from bcn.common.models import CollectedNewsItem
from bcn.contracts.collector import CollectorSourceRequest
from bcn.contracts.collector import collector_items_from_payload
from bcn.transports.http._json_client import JsonHttpServiceClient
from bcn.transports.http.routes import COLLECTOR_COLLECT_PATH


class RemoteCollectorClient(JsonHttpServiceClient):
    """Remote collector client over JSON/HTTP."""

    async def collect(self, source: str) -> list[CollectedNewsItem]:
        """Collect items from one configured upstream source remotely."""
        request = CollectorSourceRequest(source=source)
        payload = await self._post_json(COLLECTOR_COLLECT_PATH, request.to_payload())
        return collector_items_from_payload(payload)
