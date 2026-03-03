"""Role-aware LLM client for analysis, briefing, and cover generation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import logging
import random
import re
from typing import Any, TYPE_CHECKING

import httpx

from bcn.briefing.text import canonical_url_key

if TYPE_CHECKING:
    from bcn.common.config import Settings

logger = logging.getLogger(__name__)


LLM_ROLES = ("analyst", "writer", "critic", "verifier", "cover")


@dataclass(frozen=True)
class _EndpointConfig:
    base_url: str
    model: str
    provider: str = "openai_compat"
    api_key: str = ""


class LLMClient:
    """Role-aware LLM client supporting OpenAI-compatible, Gemini, and Vertex AI APIs."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: int = 120,
        *,
        provider: str = "openai_compat",
        api_key: str = "",
        role_overrides: dict[str, dict[str, str] | _EndpointConfig] | None = None,
        chat_retries: int = 12,
        retry_max_wait_seconds: float = 300.0,
        retry_jitter_min_seconds: float = 0.1,
        retry_jitter_max_seconds: float = 2.5,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self.provider = self._normalize_provider(provider)
        self.api_key = api_key or ""
        self.timeout = timeout
        self.chat_retries = max(1, int(chat_retries))
        self.retry_max_wait_seconds = max(1.0, float(retry_max_wait_seconds))
        self.retry_jitter_min_seconds = max(0.0, float(retry_jitter_min_seconds))
        self.retry_jitter_max_seconds = max(
            self.retry_jitter_min_seconds, float(retry_jitter_max_seconds)
        )
        self._default_endpoint = _EndpointConfig(
            base_url=self.base_url,
            model=self.model,
            provider=self.provider,
            api_key=self.api_key,
        )
        self._role_endpoints: dict[str, _EndpointConfig] = {}
        for role, override in (role_overrides or {}).items():
            if role not in LLM_ROLES:
                continue
            payload = (
                {
                    "base_url": override.base_url,
                    "model": override.model,
                    "provider": override.provider,
                    "api_key": override.api_key,
                }
                if isinstance(override, _EndpointConfig)
                else override
            )
            resolved = self._resolve_endpoint_override(payload)
            if resolved:
                self._role_endpoints[role] = resolved
        self._client = httpx.AsyncClient(timeout=timeout)
        self._genai_clients: dict[str, Any] = {}
        self._url_status_cache: dict[str, bool] = {}

    @classmethod
    def from_settings(cls, settings: "Settings") -> "LLMClient":
        """Build a role-aware LLM client from application settings."""
        role_overrides: dict[str, dict[str, str]] = {}
        for role in LLM_ROLES:
            role_overrides[role] = {
                "provider": cls._resolve_role_value(settings, "llm_provider", role),
                "base_url": cls._resolve_role_value(settings, "llm_base_url", role),
                "model": cls._resolve_role_value(settings, "llm_model", role),
                "api_key": cls._resolve_role_value(settings, "llm_api_key", role),
            }
        return cls(
            base_url=str(settings.llm_base_url or ""),
            model=str(settings.llm_model or ""),
            timeout=int(settings.llm_timeout),
            provider=str(settings.llm_provider or "openai_compat"),
            api_key=str(settings.llm_api_key or ""),
            role_overrides=role_overrides,
            chat_retries=int(settings.llm_chat_retries),
            retry_max_wait_seconds=float(settings.llm_retry_max_wait_seconds),
            retry_jitter_min_seconds=float(settings.llm_retry_jitter_min_seconds),
            retry_jitter_max_seconds=float(settings.llm_retry_jitter_max_seconds),
        )

    @staticmethod
    def _resolve_role_value(settings: "Settings", field: str, role: str) -> str:
        override = str(getattr(settings, f"{field}_{role}", "") or "").strip()
        base = str(getattr(settings, field, "") or "").strip()
        return override or base

    @staticmethod
    def _normalize_provider(value: str) -> str:
        provider = (value or "").strip().lower()
        if provider in {"gemini", "gemini_native", "google"}:
            return "gemini"
        if provider in {"vertexai", "vertex_ai", "vertex", "google_vertex"}:
            return "vertexai"
        return "openai_compat"

    def _resolve_endpoint_override(
        self, payload: dict[str, str] | None
    ) -> _EndpointConfig | None:
        if not payload:
            return None
        base_url = str(payload.get("base_url", "") or "").strip() or self.base_url
        model = str(payload.get("model", "") or "").strip() or self.model
        provider = self._normalize_provider(
            str(payload.get("provider", "") or self.provider)
        )
        api_key = str(payload.get("api_key", "") or "").strip() or self.api_key
        if not base_url or not model:
            return None
        return _EndpointConfig(
            base_url=base_url.rstrip("/"),
            model=model,
            provider=provider,
            api_key=api_key,
        )

    def _endpoint(self, role: str | None = None) -> _EndpointConfig:
        if role and role in self._role_endpoints:
            return self._role_endpoints[role]
        return self._default_endpoint

    def model_for_role(self, role: str) -> str:
        """Expose resolved model for trace/debug output."""
        return self._endpoint(role).model

    def endpoint_map(self) -> dict[str, dict[str, str]]:
        """Expose effective endpoint configuration by role."""
        out: dict[str, dict[str, str]] = {
            "default": self._endpoint(None).__dict__.copy(),
        }
        for role in LLM_ROLES:
            out[role] = self._endpoint(role).__dict__.copy()
        for value in out.values():
            if value.get("api_key"):
                value["api_key"] = "***"
        return out

    async def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def chat(
        self,
        system_prompt: str,
        user_content: str,
        retries: int | None = None,
    ) -> str:
        """Send a chat request using the default endpoint."""
        return await self.chat_for_role(
            role=None,
            system_prompt=system_prompt,
            user_content=user_content,
            retries=retries,
        )

    async def chat_for_role(
        self,
        *,
        role: str | None,
        system_prompt: str,
        user_content: str,
        retries: int | None = None,
        json_response: bool = False,
        tools: list[Any] | None = None,
    ) -> str:
        """Send a role-specific chat request with automatic retries."""
        endpoint = self._endpoint(role)
        max_retries = max(1, int(self.chat_retries if retries is None else retries))
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                if endpoint.provider in {"gemini", "vertexai"}:
                    return await self._chat_gemini(
                        endpoint,
                        system_prompt,
                        user_content,
                        json_response=json_response,
                        tools=tools,
                    )
                return await self._chat_openai_compat(
                    endpoint,
                    system_prompt,
                    user_content,
                    json_response=json_response,
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                retryable = status in {408, 409, 425, 429} or status >= 500
                if not retryable:
                    raise
                last_exc = exc
                if attempt < max_retries:
                    wait = self._retry_wait_seconds(attempt, exc.response)
                    logger.warning(
                        "LLM request failed (attempt %d/%d, status=%d), retrying in %.2fs",
                        attempt,
                        max_retries,
                        status,
                        wait,
                    )
                    await asyncio.sleep(wait)
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                if attempt < max_retries:
                    wait = self._retry_wait_seconds(attempt)
                    logger.warning(
                        "LLM request failed (attempt %d/%d, %s), retrying in %.2fs",
                        attempt,
                        max_retries,
                        type(exc).__name__,
                        wait,
                    )
                    await asyncio.sleep(wait)
            except Exception as exc:
                is_genai_error = type(exc).__name__ in {
                    "APIError",
                    "ClientError",
                } and "google" in getattr(type(exc), "__module__", "")
                if is_genai_error and (
                    "429" in str(exc)
                    or "RESOURCE_EXHAUSTED" in str(exc)
                    or getattr(exc, "code", 0) in {429, 500, 502, 503, 504}
                ):
                    last_exc = exc
                    if attempt < max_retries:
                        wait = self._retry_wait_seconds(attempt)
                        logger.warning(
                            "LLM request failed (attempt %d/%d, %s), retrying in %.2fs",
                            attempt,
                            max_retries,
                            type(exc).__name__,
                            wait,
                        )
                        await asyncio.sleep(wait)
                else:
                    raise
        raise (
            last_exc
            if last_exc
            else RuntimeError("LLM request failed without exception")
        )

    async def _chat_openai_compat(
        self,
        endpoint: _EndpointConfig,
        system_prompt: str,
        user_content: str,
        *,
        json_response: bool = False,
    ) -> str:
        request: dict[str, Any] = {
            "model": endpoint.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        response = await self._client.post(
            f"{endpoint.base_url}/chat/completions",
            headers=self._headers(endpoint),
            json=request,
        )
        response.raise_for_status()
        return str(response.json()["choices"][0]["message"]["content"])

    def _get_genai_client(self, endpoint: _EndpointConfig) -> Any:
        from google import genai

        key = f"{endpoint.base_url}_{endpoint.api_key}"
        if key not in self._genai_clients:
            http_options = {}
            if endpoint.base_url and "generativelanguage" not in endpoint.base_url:
                if endpoint.base_url.rstrip("/").endswith("/v1"):
                    base = endpoint.base_url.rstrip("/")[:-3]
                    http_options = {
                        "base_url": base,
                        "api_version": "v1/publishers/google",
                    }
                else:
                    http_options = {"base_url": endpoint.base_url}
            self._genai_clients[key] = genai.Client(
                api_key=endpoint.api_key or "NO_KEY",
                http_options=http_options if http_options else None,
            )
        return self._genai_clients[key]

    async def _chat_gemini(
        self,
        endpoint: _EndpointConfig,
        system_prompt: str,
        user_content: str,
        *,
        json_response: bool = False,
        tools: list[Any] | None = None,
    ) -> str:
        from google.genai import types

        client = self._get_genai_client(endpoint)
        response_mime_type = "application/json" if json_response else "text/plain"

        # Determine if we should merge prompts to bypass potential Vertex Express issues
        use_vertex = endpoint.provider == "vertexai" or self._is_gemini_vertex_endpoint(
            endpoint
        )
        if use_vertex:
            contents = f"System instructions:\n{system_prompt}\n\nUser request:\n{user_content}"
            config = types.GenerateContentConfig(
                response_mime_type=response_mime_type,
                tools=tools,
            )
        else:
            contents = user_content
            config = types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type=response_mime_type,
                tools=tools,
            )

        response = await client.aio.models.generate_content(
            model=endpoint.model,
            contents=contents,
            config=config,
        )
        if not response.text:
            raise RuntimeError("Gemini response did not include a text part")
        return response.text

    async def generate_gemini_image(
        self,
        endpoint: _EndpointConfig,
        prompt_text: str,
    ) -> tuple[str, bytes]:
        from google.genai import types

        client = self._get_genai_client(endpoint)

        config = types.GenerateImagesConfig(
            number_of_images=1,
            output_mime_type="image/png",
            aspect_ratio="16:9",
        )
        response = await client.aio.models.generate_images(
            model=endpoint.model,
            prompt=prompt_text,
            config=config,
        )

        for generated_image in response.generated_images:
            if getattr(generated_image, "image", None) and getattr(
                generated_image.image, "image_bytes", None
            ):
                return "image/png", generated_image.image.image_bytes

        raise RuntimeError("Gemini image response did not include image bytes")

    @staticmethod
    def _is_gemini_vertex_endpoint(endpoint: _EndpointConfig) -> bool:
        base = endpoint.base_url.lower()
        return (
            "aiplatform.googleapis.com" in base or "/publishers/google/models/" in base
        )

    @staticmethod
    def parse_json_response(raw_text: str) -> Any:
        """Parse JSON from model output with fence stripping and raw-decode fallback."""
        cleaned = re.sub(r"```json\s*", "", raw_text or "", flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```\s*", "", cleaned).strip()
        if not cleaned:
            raise json.JSONDecodeError("empty response", cleaned, 0)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            for idx, ch in enumerate(cleaned):
                if ch not in "{[":
                    continue
                try:
                    parsed, _ = decoder.raw_decode(cleaned[idx:])
                    return parsed
                except json.JSONDecodeError:
                    continue
        raise json.JSONDecodeError("unable to parse json", cleaned, 0)

    _normalized_url_key = staticmethod(canonical_url_key)

    @staticmethod
    def _headers(endpoint: _EndpointConfig) -> dict[str, str]:
        if endpoint.api_key:
            return {"Authorization": f"Bearer {endpoint.api_key}"}
        return {}

    def _retry_wait_seconds(
        self,
        attempt: int,
        response: httpx.Response | None = None,
    ) -> float:
        wait = min(self.retry_max_wait_seconds, float(2**attempt))
        wait += random.uniform(self.retry_jitter_min_seconds, self.retry_jitter_max_seconds)
        retry_after = self._parse_retry_after_seconds(response)
        if retry_after is not None:
            wait = max(wait, min(self.retry_max_wait_seconds, retry_after))
        return wait

    @staticmethod
    def _parse_retry_after_seconds(response: httpx.Response | None) -> float | None:
        if response is None:
            return None
        raw = response.headers.get("Retry-After", "").strip()
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            retry_at = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
