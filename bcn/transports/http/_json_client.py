"""Shared JSON-over-HTTP client helpers for BCN service adapters."""

from __future__ import annotations

import json
from typing import Any

import httpx


class JsonHttpServiceClient:
    """Minimal async JSON client used by remote BCN service adapters."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: int,
        auth_token: str = "",
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        headers = {"Accept": "application/json"}
        token = str(auth_token or "").strip()
        if token:
            headers["X-BCN-Service-Token"] = token
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=max(1, int(timeout_seconds)),
            headers=headers,
        )

    async def close(self) -> None:
        """Release the underlying HTTP client."""
        await self._client.aclose()

    async def _get_json(self, path: str) -> dict[str, Any]:
        """Send one GET request and return a decoded JSON object."""
        response = await self._client.get(path)
        response.raise_for_status()
        return self._decode_json(response)

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one POST request with JSON and return a decoded response."""
        response = await self._client.post(path, json=payload)
        response.raise_for_status()
        return self._decode_json(response)

    @staticmethod
    def _decode_json(response: httpx.Response) -> dict[str, Any]:
        """Decode a JSON object response or raise a helpful error."""
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Expected JSON response from {response.request.url}, got invalid payload"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Expected JSON object response from {response.request.url}"
            )
        return payload
