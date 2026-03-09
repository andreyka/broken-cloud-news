"""HTTP client for remote distributor service deployments."""

from __future__ import annotations

from bcn.contracts.distributor import DeliveryRequest
from bcn.contracts.distributor import DeliveryResult
from bcn.contracts.distributor import delivery_request_to_payload
from bcn.contracts.distributor import delivery_result_from_payload
from bcn.transports.http._json_client import JsonHttpServiceClient
from bcn.transports.http.routes import DISTRIBUTOR_DELIVER_PATH


class RemoteDistributorClient(JsonHttpServiceClient):
    """Remote distributor client over JSON/HTTP."""

    async def deliver(self, request: DeliveryRequest) -> DeliveryResult:
        """Deliver one briefing payload through a remote distributor service."""
        payload = await self._post_json(
            DISTRIBUTOR_DELIVER_PATH,
            delivery_request_to_payload(request),
        )
        result = delivery_result_from_payload(payload)
        if result is None:
            raise RuntimeError("Distributor service returned an invalid delivery payload")
        return result
