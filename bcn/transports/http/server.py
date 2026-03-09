"""Minimal JSON-over-HTTP servers for BCN deployable services."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import json
import logging
from typing import Any
from urllib.parse import urlsplit

from bcn.common.config import Settings
from bcn.contracts.review import critique_request_from_payload
from bcn.contracts.review import verification_request_from_payload
from bcn.contracts.writer import WriterArtifactRequest
from bcn.contracts.writer import WriterDraftEvaluationRequest
from bcn.contracts.writer import WriterReleaseCandidateRequest
from bcn.contracts.writer import WriterSelectionRequest
from bcn.contracts.writer import WriterSimulationRequest
from bcn.persistence.runtime import close_pool
from bcn.service_registry import build_local_critic_evaluator
from bcn.service_registry import build_local_verifier_evaluator
from bcn.service_registry import build_local_writer_workflow
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


class ComponentHTTPServer(ThreadingHTTPServer):
    """Threaded JSON API server for one BCN component."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        component: str,
        settings: Settings,
    ) -> None:
        self.component = component
        self.settings = settings
        self.get_routes, self.post_routes = _build_routes(component, settings)
        super().__init__(server_address, ComponentRequestHandler)


class ComponentRequestHandler(BaseHTTPRequestHandler):
    """Handle JSON requests for one component server."""

    server: ComponentHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler naming
        self._handle(get_only=True)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler naming
        self._handle(get_only=False)

    def log_message(self, format: str, *args: object) -> None:
        logger.info(
            "%s %s - %s",
            self.server.component,
            self.address_string(),
            format % args,
        )

    def _handle(self, *, get_only: bool) -> None:
        path = urlsplit(self.path).path or "/"
        if path in _route_aliases(HEALTH_PATH):
            self._write_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "component": self.server.component,
                },
            )
            return

        if not self._is_authorized():
            self._write_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "missing or invalid service auth token"},
            )
            return

        if get_only:
            handler = self.server.get_routes.get(path)
            if handler is None:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": f"Unknown GET endpoint: {path}"},
                )
                return
            try:
                payload = asyncio.run(handler())
            except Exception:
                logger.exception(
                    "Unhandled %s GET %s failure",
                    self.server.component,
                    path,
                )
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "internal server error"},
                )
                return
            self._write_json(HTTPStatus.OK, payload)
            return

        handler = self.server.post_routes.get(path)
        if handler is None:
            self._write_json(
                HTTPStatus.NOT_FOUND,
                {"error": f"Unknown POST endpoint: {path}"},
            )
            return

        try:
            payload = self._read_json_body()
            body = asyncio.run(handler(payload))
        except ValueError as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception:
            logger.exception(
                "Unhandled %s POST %s failure",
                self.server.component,
                path,
            )
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal server error"},
            )
            return

        self._write_json(HTTPStatus.OK, body)

    def _read_json_body(self) -> dict[str, Any]:
        """Read and validate a JSON object request body."""
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        """Send one JSON response with a fixed content length."""
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _is_authorized(self) -> bool:
        """Return whether the request satisfies configured service auth."""
        return _headers_authorized(
            expected_token=str(self.server.settings.service_auth_token or ""),
            header_token=str(self.headers.get("X-BCN-Service-Token", "") or ""),
            authorization_header=str(self.headers.get("Authorization", "") or ""),
        )


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


def create_component_http_server(
    settings: Settings,
    *,
    component: str,
    host: str,
    port: int,
) -> ComponentHTTPServer:
    """Create one threaded HTTP server for a BCN component."""
    return ComponentHTTPServer((host, int(port)), component, settings)


def serve_component_http(
    settings: Settings,
    *,
    component: str,
    host: str,
    port: int,
) -> None:
    """Serve one BCN component over JSON/HTTP until interrupted."""
    server = create_component_http_server(
        settings,
        component=component,
        host=host,
        port=port,
    )
    logger.info(
        "Serving %s service on http://%s:%d",
        component,
        server.server_address[0],
        server.server_address[1],
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        asyncio.run(close_pool())
