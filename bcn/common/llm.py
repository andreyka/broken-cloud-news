"""Role-aware LLM client for analysis, briefing, and cover generation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from email.utils import parsedate_to_datetime
import inspect
import json
import logging
import random
import re
from typing import Any, Callable, TYPE_CHECKING

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
        self._genai_client_lock = asyncio.Lock()
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
        async with self._genai_client_lock:
            genai_clients = tuple(self._genai_clients.values())
            self._genai_clients.clear()

        for client in genai_clients:
            try:
                async_client = getattr(client, "aio", None)
                async_close = getattr(async_client, "aclose", None)
                if callable(async_close):
                    result = async_close()
                    if inspect.isawaitable(result):
                        await result
                    continue

                close = getattr(client, "close", None)
                if callable(close):
                    result = close()
                    if inspect.isawaitable(result):
                        await result
            except Exception:
                logger.warning("Failed to close cached Gemini client cleanly", exc_info=True)
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
                    tools=tools,
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
        tools: list[Any] | None = None,
    ) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        request: dict[str, Any] = {
            "model": endpoint.model,
            "messages": messages,
        }
        self._apply_openai_compat_model_overrides(endpoint, request)
        if tools:
            tool_schemas = [self._build_openai_tool_schema(tool) for tool in tools]
            request["tools"] = tool_schemas
            request["tool_choice"] = "auto"

            tool_map: dict[str, Callable[..., Any]] = {
                str(tool.__name__): tool for tool in tools if callable(tool)
            }
            for _ in range(8):
                response = await self._client.post(
                    f"{endpoint.base_url}/chat/completions",
                    headers=self._headers(endpoint),
                    json=request,
                )
                response.raise_for_status()
                choice = (response.json().get("choices") or [{}])[0]
                message = choice.get("message") or {}
                tool_calls = list(message.get("tool_calls") or [])
                if not tool_calls:
                    return self._extract_openai_content(message)

                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "tool_calls": tool_calls,
                }
                if "content" in message:
                    assistant_message["content"] = message.get("content")
                messages.append(assistant_message)

                for tool_call in tool_calls:
                    tool_response = await self._invoke_openai_tool_call(
                        tool_call, tool_map
                    )
                    tool_call_id = str(tool_call.get("id") or "")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "content": tool_response,
                        }
                    )
                request["messages"] = messages

            raise RuntimeError(
                "OpenAI-compatible tool call loop exceeded maximum iterations"
            )

        response = await self._client.post(
            f"{endpoint.base_url}/chat/completions",
            headers=self._headers(endpoint),
            json=request,
        )
        response.raise_for_status()
        return self._extract_openai_content(response.json()["choices"][0]["message"])

    @staticmethod
    def _apply_openai_compat_model_overrides(
        endpoint: _EndpointConfig, request: dict[str, Any]
    ) -> None:
        model_name = str(endpoint.model or "").strip().lower()

        # Qwen 3/3.6 models default to thinking mode on vLLM/OpenAI-compatible APIs.
        # BCN expects final assistant text in `message.content`, so request
        # non-thinking mode explicitly for these models.
        if model_name.startswith("qwen/") and "qwen3" in model_name:
            chat_template_kwargs = request.get("chat_template_kwargs")
            if not isinstance(chat_template_kwargs, dict):
                chat_template_kwargs = {}
                request["chat_template_kwargs"] = chat_template_kwargs
            chat_template_kwargs.setdefault("enable_thinking", False)

    @staticmethod
    def _build_openai_tool_schema(tool: Callable[..., Any]) -> dict[str, Any]:
        signature = inspect.signature(tool)
        properties: dict[str, dict[str, str]] = {}
        required: list[str] = []

        for parameter in signature.parameters.values():
            if parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            properties[parameter.name] = {
                "type": LLMClient._json_schema_type_for_annotation(
                    parameter.annotation
                )
            }
            if parameter.default is inspect.Parameter.empty:
                required.append(parameter.name)

        return {
            "type": "function",
            "function": {
                "name": tool.__name__,
                "description": inspect.getdoc(tool) or tool.__name__,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    @staticmethod
    def _json_schema_type_for_annotation(annotation: Any) -> str:
        if annotation in {int}:
            return "integer"
        if annotation in {float}:
            return "number"
        if annotation in {bool}:
            return "boolean"
        return "string"

    async def _invoke_openai_tool_call(
        self,
        tool_call: dict[str, Any],
        tool_map: dict[str, Callable[..., Any]],
    ) -> str:
        function_payload = tool_call.get("function") or {}
        tool_name = str(function_payload.get("name") or "").strip()
        if not tool_name or tool_name not in tool_map:
            return f"Tool execution failed: unknown tool '{tool_name}'."

        raw_arguments = function_payload.get("arguments")
        try:
            arguments = (
                json.loads(raw_arguments)
                if isinstance(raw_arguments, str) and raw_arguments.strip()
                else {}
            )
        except json.JSONDecodeError as exc:
            return f"Tool execution failed: invalid JSON arguments ({exc})."
        if not isinstance(arguments, dict):
            return "Tool execution failed: tool arguments must decode to an object."

        try:
            result = tool_map[tool_name](**arguments)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            logger.warning("OpenAI-compatible tool execution failed: %s", exc)
            return f"Tool execution failed: {exc}"

        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)

    @staticmethod
    def _extract_openai_content(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "output_text" and isinstance(
                    block.get("text"), str
                ):
                    parts.append(block["text"])
            return "\n".join(part for part in parts if part).strip()
        return ""

    async def _get_genai_client(self, endpoint: _EndpointConfig) -> Any:
        from google import genai

        key = f"{endpoint.base_url}_{endpoint.api_key}"
        existing = self._genai_clients.get(key)
        if existing is not None:
            return existing

        async with self._genai_client_lock:
            existing = self._genai_clients.get(key)
            if existing is not None:
                return existing
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
            client = genai.Client(
                api_key=endpoint.api_key or "NO_KEY",
                http_options=http_options if http_options else None,
            )
            self._genai_clients[key] = client
            return client

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

        client = await self._get_genai_client(endpoint)
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

        client = await self._get_genai_client(endpoint)

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
