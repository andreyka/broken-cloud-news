"""HTTP clients for remote critic and verifier deployments."""

from __future__ import annotations

from typing import Any

from bcn.contracts.review import CritiqueRequest
from bcn.contracts.review import VerificationRequest
from bcn.contracts.review import critique_request_to_payload
from bcn.contracts.review import verification_request_to_payload
from bcn.transports.http._json_client import JsonHttpServiceClient
from bcn.transports.http.routes import CRITIC_EVALUATE_PATH
from bcn.transports.http.routes import VERIFIER_EVALUATE_PATH


class RemoteCriticClient(JsonHttpServiceClient):
    """Remote critic evaluator over JSON/HTTP."""

    async def evaluate(self, request: CritiqueRequest) -> dict[str, Any]:
        """Evaluate one critique request via the remote critic service."""
        return await self._post_json(
            CRITIC_EVALUATE_PATH,
            critique_request_to_payload(request),
        )


class RemoteVerifierClient(JsonHttpServiceClient):
    """Remote verifier evaluator over JSON/HTTP."""

    async def evaluate(self, request: VerificationRequest) -> dict[str, Any]:
        """Evaluate one verification request via the remote verifier service."""
        return await self._post_json(
            VERIFIER_EVALUATE_PATH,
            verification_request_to_payload(request),
        )
