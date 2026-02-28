"""Role-aware LLM client for analysis, briefing, and cover generation."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import logging
import random
import re
from typing import Any, TYPE_CHECKING
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlparse

import httpx

from bcn.common.models import AnalysisResult

if TYPE_CHECKING:
    from bcn.common.config import Settings

logger = logging.getLogger(__name__)

_TRACKING_PARAM_NAMES = frozenset({
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "msclkid",
    "rb_clickid",
    "s_cid",
    "vero_conv",
    "vero_id",
    "yclid",
})

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
        role_overrides: dict[str, dict[str, str] | _EndpointConfig] |
        None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self.provider = self._normalize_provider(provider)
        self.api_key = api_key or ""
        self.timeout = timeout
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
            payload = ({
                "base_url": override.base_url,
                "model": override.model,
                "provider": override.provider,
                "api_key": override.api_key,
            } if isinstance(override, _EndpointConfig) else override)
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
                "provider":
                    cls._resolve_role_value(settings, "llm_provider", role),
                "base_url":
                    cls._resolve_role_value(settings, "llm_base_url", role),
                "model":
                    cls._resolve_role_value(settings, "llm_model", role),
                "api_key":
                    cls._resolve_role_value(settings, "llm_api_key", role),
            }
        return cls(
            base_url=str(settings.llm_base_url or ""),
            model=str(settings.llm_model or ""),
            timeout=int(settings.llm_timeout),
            provider=str(settings.llm_provider or "openai_compat"),
            api_key=str(settings.llm_api_key or ""),
            role_overrides=role_overrides,
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
            self, payload: dict[str, str] | None) -> _EndpointConfig | None:
        if not payload:
            return None
        base_url = str(payload.get("base_url", "") or
                       "").strip() or self.base_url
        model = str(payload.get("model", "") or "").strip() or self.model
        provider = self._normalize_provider(
            str(payload.get("provider", "") or self.provider))
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

    async def chat(
        self,
        system_prompt: str,
        user_content: str,
        retries: int = 3,
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
        retries: int = 8,
        json_response: bool = False,
        tools: list[Any] | None = None,
    ) -> str:
        """Send a role-specific chat request with automatic retries."""
        endpoint = self._endpoint(role)
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
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
                if attempt < retries:
                    wait = min(60.0, (2 ** attempt)) + random.uniform(0.1, 1.5)
                    logger.warning(
                        "LLM request failed (attempt %d/%d, status=%d), retrying in %.2fs",
                        attempt,
                        retries,
                        status,
                        wait,
                    )
                    await asyncio.sleep(wait)
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                if attempt < retries:
                    wait = min(60.0, (2 ** attempt)) + random.uniform(0.1, 1.5)
                    logger.warning(
                        "LLM request failed (attempt %d/%d, %s), retrying in %.2fs",
                        attempt,
                        retries,
                        type(exc).__name__,
                        wait,
                    )
                    await asyncio.sleep(wait)
            except Exception as exc:
                is_genai_error = type(exc).__name__ in {"APIError", "ClientError"} and "google" in getattr(type(exc), "__module__", "")
                if is_genai_error and ("429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc) or getattr(exc, "code", 0) in {429, 500, 502, 503, 504}):
                    last_exc = exc
                    if attempt < retries:
                        wait = min(60.0, (2 ** attempt)) + random.uniform(0.1, 1.5)
                        logger.warning(
                            "LLM request failed (attempt %d/%d, %s), retrying in %.2fs",
                            attempt,
                            retries,
                            type(exc).__name__,
                            wait,
                        )
                        await asyncio.sleep(wait)
                else:
                    raise
        raise last_exc if last_exc else RuntimeError(
            "LLM request failed without exception")

    async def _chat_openai_compat(
        self,
        endpoint: _EndpointConfig,
        system_prompt: str,
        user_content: str,
        *,
        json_response: bool = False,
    ) -> str:
        request: dict[str, Any] = {
            "model":
                endpoint.model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": user_content
                },
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
                        "api_version": "v1/publishers/google"
                    }
                else:
                    http_options = {"base_url": endpoint.base_url}
            self._genai_clients[key] = genai.Client(
                api_key=endpoint.api_key or "NO_KEY",
                http_options=http_options if http_options else None)
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
            endpoint)
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

        config = types.GenerateContentConfig(response_modalities=["IMAGE"])
        response = await client.aio.models.generate_content(
            model=endpoint.model,
            contents=prompt_text,
            config=config,
        )

        for candidate in response.candidates:
            if candidate.content and candidate.content.parts:
                part = candidate.content.parts[0]
                if getattr(part, "inline_data", None) and part.inline_data.data:
                    mime = getattr(part.inline_data, "mime_type",
                                   "image/png") or "image/png"
                    return mime, part.inline_data.data

        raise RuntimeError(
            "Gemini image response did not include inline image data")

    @staticmethod
    def _is_gemini_vertex_endpoint(endpoint: _EndpointConfig) -> bool:
        base = endpoint.base_url.lower()
        return "aiplatform.googleapis.com" in base or "/publishers/google/models/" in base

    @staticmethod
    def parse_json_response(raw_text: str) -> Any:
        """Parse JSON from model output with fence stripping and raw-decode fallback."""
        cleaned = re.sub(r"```json\s*", "", raw_text or "",
                         flags=re.IGNORECASE).strip()
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

    @staticmethod
    def _normalized_url_key(url: str) -> str:
        if not url:
            return ""
        trimmed = str(url).strip().rstrip(").,;!?")
        if not trimmed:
            return ""
        try:
            parsed = urlparse(trimmed)
        except Exception:
            return trimmed
        scheme = parsed.scheme.lower() or "https"
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = parsed.path.rstrip("/")
        query_params: list[tuple[str, str]] = []
        for raw_key, raw_value in parse_qsl(parsed.query,
                                            keep_blank_values=True):
            key = raw_key.lower()
            if key.startswith("utm_") or key.startswith(
                    "mc_") or key in _TRACKING_PARAM_NAMES:
                continue
            query_params.append((key, raw_value))
        query_params.sort()
        query = urlencode(query_params, doseq=True)
        if query:
            return f"{scheme}://{netloc}{path}?{query}"
        return f"{scheme}://{netloc}{path}"

    @staticmethod
    def _headers(endpoint: _EndpointConfig) -> dict[str, str]:
        if endpoint.api_key:
            return {"Authorization": f"Bearer {endpoint.api_key}"}
        return {}
