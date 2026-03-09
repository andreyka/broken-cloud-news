"""ASGI JSON-over-HTTP servers for BCN deployable services."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import asynccontextmanager
import json
import logging
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from bcn.common.component_settings import default_service_port
from bcn.common.config import Settings
from bcn.contracts.analyst import AnalystItemRequest
from bcn.contracts.analyst import analyzed_item_to_payload
from bcn.contracts.collector import CollectorSourceRequest
from bcn.contracts.collector import collector_items_to_payload
from bcn.contracts.collector import validate_collection_source
from bcn.contracts.review import critique_request_from_payload
from bcn.contracts.review import verification_request_from_payload
from bcn.contracts.writer import WriterArtifactRequest
from bcn.contracts.writer import WriterDraftEvaluationRequest
from bcn.contracts.writer import WriterReleaseCandidateRequest
from bcn.contracts.writer import WriterSelectionRequest
from bcn.contracts.writer import WriterSimulationRequest
from bcn.persistence.runtime import close_pool
from bcn.service_registry import build_local_analyst_workflow
from bcn.service_registry import build_local_collector_workflow
from bcn.service_registry import build_local_critic_evaluator
from bcn.service_registry import build_local_verifier_evaluator
from bcn.service_registry import build_local_writer_workflow
from bcn.transports.http.routes import ANALYST_ANALYZE_ITEM_PATH
from bcn.transports.http.routes import COLLECTOR_COLLECT_PATH
from bcn.transports.http.routes import CRITIC_EVALUATE_PATH
from bcn.transports.http.routes import HEALTH_PATH
from bcn.transports.http.routes import VERIFIER_EVALUATE_PATH
from bcn.transports.http.routes import WRITER_BUILD_ARTIFACT_PATH
from bcn.transports.http.routes import WRITER_EVALUATE_EXISTING_PATH
from bcn.transports.http.routes import WRITER_GENERATE_CANDIDATE_PATH
from bcn.transports.http.routes import WRITER_SELECT_ITEMS_PATH
from bcn.transports.http.routes import WRITER_SIMULATE_PATH
from bcn.transports.http.routes import WRITER_TRACE_METADATA_PATH

logger = logging.getLogger(__name__)

JsonHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
GetHandler = Callable[[], Awaitable[dict[str, Any]]]


def _route_aliases(path: str) -> tuple[str, ...]:
    """Return canonical and compatibility aliases for one API path."""
    normalized = str(path or "").strip()
    if not normalized:
        return ("",)
    if normalized.startswith("/v1/"):
        legacy = normalized[len("/v1") :]
        return (normalized, legacy or "/")
    return (normalized,)


def _headers_authorized(
    *,
    expected_token: str,
    header_token: str,
    authorization_header: str,
) -> bool:
    """Return whether request headers satisfy the configured shared token."""
    expected = str(expected_token or "").strip()
    if not expected:
        return True

    direct = str(header_token or "").strip()
    if direct == expected:
        return True

    bearer = str(authorization_header or "").strip()
    return bearer == f"Bearer {expected}"


def _build_routes(
    component: str,
    settings: Settings,
) -> tuple[dict[str, GetHandler], dict[str, JsonHandler]]:
    """Build component-local HTTP routes."""
    if component == "writer":
        return _writer_routes(settings)
    if component == "critic":
        return _critic_routes(settings)
    if component == "verifier":
        return _verifier_routes(settings)
    if component == "collector":
        return _collector_routes(settings)
    if component == "analyst":
        return _analyst_routes(settings)
    raise ValueError(f"Unsupported component: {component}")


def _writer_routes(settings: Settings) -> tuple[dict[str, GetHandler], dict[str, JsonHandler]]:
    """Return writer HTTP routes."""

    async def _trace_metadata() -> dict[str, Any]:
        service = build_local_writer_workflow(settings)
        try:
            return (await service.get_trace_metadata()).to_payload()
        finally:
            await service.close()

    async def _select_items(payload: dict[str, Any]) -> dict[str, Any]:
        request = WriterSelectionRequest.from_payload(payload)
        if not request.workflow_mode:
            raise ValueError("workflow_mode is required.")
        service = build_local_writer_workflow(settings)
        try:
            return await service.select_items_for_workflow(
                item_dicts=request.item_dicts,
                workflow_mode=request.workflow_mode,
            )
        finally:
            await service.close()

    async def _evaluate_existing(payload: dict[str, Any]) -> dict[str, Any]:
        request = WriterDraftEvaluationRequest.from_payload(payload)
        if not request.mode:
            raise ValueError("mode is required.")
        service = build_local_writer_workflow(settings)
        try:
            return await service.evaluate_existing_markdown(
                markdown=request.markdown,
                selected_items=request.selected_items,
                history=request.history,
                mode=request.mode,
            )
        finally:
            await service.close()

    async def _generate_candidate(payload: dict[str, Any]) -> dict[str, Any]:
        request = WriterReleaseCandidateRequest.from_payload(payload)
        if not request.mode:
            raise ValueError("mode is required.")
        service = build_local_writer_workflow(settings)
        try:
            return await service.generate_release_candidate(
                selected_items=request.selected_items,
                history=request.history,
                mode=request.mode,
            )
        finally:
            await service.close()

    async def _build_artifact(payload: dict[str, Any]) -> dict[str, Any]:
        request = WriterArtifactRequest.from_payload(payload)
        if not request.mode:
            raise ValueError("mode is required.")
        service = build_local_writer_workflow(settings)
        try:
            return await service.build_release_artifact(
                briefing_body=request.briefing_body,
                selected_items=request.selected_items,
                mode=request.mode,
            )
        finally:
            await service.close()

    async def _simulate(payload: dict[str, Any]) -> dict[str, Any]:
        request = WriterSimulationRequest.from_payload(payload)
        service = build_local_writer_workflow(settings)
        try:
            markdown, meta = await service.simulate_briefing_body(
                request.items,
                request.recent_briefings,
                apply_critic_rewrites=request.apply_critic_rewrites,
            )
        finally:
            await service.close()
        return {
            "markdown": markdown,
            "meta": dict(meta),
        }

    get_routes: dict[str, GetHandler] = {}
    for path in _route_aliases(WRITER_TRACE_METADATA_PATH):
        get_routes[path] = _trace_metadata

    post_routes: dict[str, JsonHandler] = {}
    for path in _route_aliases(WRITER_SELECT_ITEMS_PATH):
        post_routes[path] = _select_items
    for path in _route_aliases(WRITER_EVALUATE_EXISTING_PATH):
        post_routes[path] = _evaluate_existing
    for path in _route_aliases(WRITER_GENERATE_CANDIDATE_PATH):
        post_routes[path] = _generate_candidate
    for path in _route_aliases(WRITER_BUILD_ARTIFACT_PATH):
        post_routes[path] = _build_artifact
    for path in _route_aliases(WRITER_SIMULATE_PATH):
        post_routes[path] = _simulate
    return get_routes, post_routes


def _critic_routes(settings: Settings) -> tuple[dict[str, GetHandler], dict[str, JsonHandler]]:
    """Return critic HTTP routes."""

    async def _evaluate(payload: dict[str, Any]) -> dict[str, Any]:
        request = critique_request_from_payload(payload)
        if request is None:
            raise ValueError("draft_markdown is required.")
        service = build_local_critic_evaluator(settings)
        try:
            return await service.evaluate(request)
        finally:
            await service.close()

    post_routes: dict[str, JsonHandler] = {}
    for path in _route_aliases(CRITIC_EVALUATE_PATH):
        post_routes[path] = _evaluate
    return {}, post_routes


def _verifier_routes(
    settings: Settings,
) -> tuple[dict[str, GetHandler], dict[str, JsonHandler]]:
    """Return verifier HTTP routes."""

    async def _evaluate(payload: dict[str, Any]) -> dict[str, Any]:
        request = verification_request_from_payload(payload)
        if request is None:
            raise ValueError("draft_markdown is required.")
        service = build_local_verifier_evaluator(settings)
        try:
            return await service.evaluate(request)
        finally:
            await service.close()

    post_routes: dict[str, JsonHandler] = {}
    for path in _route_aliases(VERIFIER_EVALUATE_PATH):
        post_routes[path] = _evaluate
    return {}, post_routes


def _collector_routes(
    settings: Settings,
) -> tuple[dict[str, GetHandler], dict[str, JsonHandler]]:
    """Return collector HTTP routes."""

    async def _collect(payload: dict[str, Any]) -> dict[str, Any]:
        request = CollectorSourceRequest.from_payload(payload)
        source = validate_collection_source(request.source)
        service = build_local_collector_workflow(settings)
        try:
            items = await service.collect(source)
        finally:
            await service.close()
        return collector_items_to_payload(items)

    post_routes: dict[str, JsonHandler] = {}
    for path in _route_aliases(COLLECTOR_COLLECT_PATH):
        post_routes[path] = _collect
    return {}, post_routes


def _analyst_routes(
    settings: Settings,
) -> tuple[dict[str, GetHandler], dict[str, JsonHandler]]:
    """Return analyst HTTP routes."""

    async def _analyze_item(payload: dict[str, Any]) -> dict[str, Any]:
        request = AnalystItemRequest.from_payload(payload)
        if not request.item:
            raise ValueError("item is required.")
        service = build_local_analyst_workflow(settings)
        try:
            update = await service.analyze_item(request.item)
        finally:
            await service.close()
        return analyzed_item_to_payload(update)

    post_routes: dict[str, JsonHandler] = {}
    for path in _route_aliases(ANALYST_ANALYZE_ITEM_PATH):
        post_routes[path] = _analyze_item
    return {}, post_routes


async def _read_json_body(request: Request) -> dict[str, Any]:
    """Decode one request body as a JSON object."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Request body must be valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")
    return payload


def create_component_http_app(
    settings: Settings,
    *,
    component: str,
) -> Starlette:
    """Create one ASGI app for a BCN component."""
    normalized_component = str(component or "").strip().lower()
    get_routes, post_routes = _build_routes(normalized_component, settings)

    @asynccontextmanager
    async def _lifespan(_: Starlette):
        try:
            yield
        finally:
            await close_pool()

    async def _health(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "component": normalized_component})

    async def _handle_get(request: Request) -> JSONResponse:
        if not _headers_authorized(
            expected_token=str(settings.service_auth_token or ""),
            header_token=str(request.headers.get("X-BCN-Service-Token", "") or ""),
            authorization_header=str(request.headers.get("Authorization", "") or ""),
        ):
            return JSONResponse(
                {"error": "missing or invalid service auth token"},
                status_code=401,
            )
        handler = get_routes.get(request.url.path or "/")
        if handler is None:
            return JSONResponse(
                {"error": f"Unknown GET endpoint: {request.url.path}"},
                status_code=404,
            )
        try:
            payload = await handler()
        except Exception:
            logger.exception(
                "Unhandled %s GET %s failure",
                normalized_component,
                request.url.path,
            )
            return JSONResponse({"error": "internal server error"}, status_code=500)
        return JSONResponse(payload)

    async def _handle_post(request: Request) -> JSONResponse:
        if not _headers_authorized(
            expected_token=str(settings.service_auth_token or ""),
            header_token=str(request.headers.get("X-BCN-Service-Token", "") or ""),
            authorization_header=str(request.headers.get("Authorization", "") or ""),
        ):
            return JSONResponse(
                {"error": "missing or invalid service auth token"},
                status_code=401,
            )
        handler = post_routes.get(request.url.path or "/")
        if handler is None:
            return JSONResponse(
                {"error": f"Unknown POST endpoint: {request.url.path}"},
                status_code=404,
            )
        try:
            payload = await _read_json_body(request)
            body = await handler(payload)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception:
            logger.exception(
                "Unhandled %s POST %s failure",
                normalized_component,
                request.url.path,
            )
            return JSONResponse({"error": "internal server error"}, status_code=500)
        return JSONResponse(body)

    routes = [Route(path, _health, methods=["GET"]) for path in _route_aliases(HEALTH_PATH)]
    routes.extend(Route(path, _handle_get, methods=["GET"]) for path in get_routes)
    routes.extend(Route(path, _handle_post, methods=["POST"]) for path in post_routes)
    return Starlette(routes=routes, lifespan=_lifespan)


def serve_component_http(
    settings: Settings,
    *,
    component: str,
    host: str,
    port: int,
) -> None:
    """Serve one BCN component over JSON/HTTP until interrupted."""
    import uvicorn

    normalized_component = str(component or "").strip().lower()
    bind_port = int(port) if int(port) > 0 else default_service_port(normalized_component)
    app = create_component_http_app(settings, component=normalized_component)
    logger.info(
        "Serving %s service on http://%s:%d",
        normalized_component,
        host,
        bind_port,
    )
    uvicorn.run(app, host=host, port=bind_port, log_level="info")


__all__ = [
    "_build_routes",
    "_headers_authorized",
    "create_component_http_app",
    "serve_component_http",
]
