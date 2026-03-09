"""HTTP client for remote writer workflow deployments."""

from __future__ import annotations

from typing import Any

from bcn.contracts.services import WriterTraceMetadata
from bcn.contracts.writer import WriterArtifactRequest
from bcn.contracts.writer import WriterDraftEvaluationRequest
from bcn.contracts.writer import WriterReleaseCandidateRequest
from bcn.contracts.writer import WriterSelectionRequest
from bcn.contracts.writer import WriterSimulationRequest
from bcn.transports.http._json_client import JsonHttpServiceClient
from bcn.transports.http.routes import WRITER_BUILD_ARTIFACT_PATH
from bcn.transports.http.routes import WRITER_EVALUATE_EXISTING_PATH
from bcn.transports.http.routes import WRITER_GENERATE_CANDIDATE_PATH
from bcn.transports.http.routes import WRITER_SELECT_ITEMS_PATH
from bcn.transports.http.routes import WRITER_SIMULATE_PATH
from bcn.transports.http.routes import WRITER_TRACE_METADATA_PATH


class RemoteWriterWorkflowClient(JsonHttpServiceClient):
    """Remote writer workflow client over JSON/HTTP."""

    async def get_trace_metadata(self) -> WriterTraceMetadata:
        """Fetch writer trace metadata from the remote writer service."""
        payload = await self._get_json(WRITER_TRACE_METADATA_PATH)
        return WriterTraceMetadata.from_payload(payload)

    async def select_items_for_workflow(
        self,
        item_dicts: list[dict[str, Any]],
        workflow_mode: str,
    ) -> dict[str, Any]:
        """Select items remotely for one workflow mode."""
        request = WriterSelectionRequest(
            item_dicts=list(item_dicts),
            workflow_mode=workflow_mode,
        )
        return await self._post_json(WRITER_SELECT_ITEMS_PATH, request.to_payload())

    async def evaluate_existing_markdown(
        self,
        *,
        markdown: str,
        selected_items: list[dict[str, Any]],
        history: list[dict[str, Any]],
        mode: str,
    ) -> dict[str, Any]:
        """Evaluate one existing markdown draft remotely."""
        request = WriterDraftEvaluationRequest(
            markdown=markdown,
            selected_items=list(selected_items),
            history=list(history),
            mode=mode,
        )
        return await self._post_json(
            WRITER_EVALUATE_EXISTING_PATH,
            request.to_payload(),
        )

    async def generate_release_candidate(
        self,
        *,
        selected_items: list[dict[str, Any]],
        history: list[dict[str, Any]],
        mode: str,
    ) -> dict[str, Any]:
        """Generate one remote release candidate."""
        request = WriterReleaseCandidateRequest(
            selected_items=list(selected_items),
            history=list(history),
            mode=mode,
        )
        return await self._post_json(
            WRITER_GENERATE_CANDIDATE_PATH,
            request.to_payload(),
        )

    async def build_release_artifact(
        self,
        *,
        briefing_body: str,
        selected_items: list[dict[str, Any]],
        mode: str,
    ) -> dict[str, str]:
        """Build publishable artifact assets remotely."""
        request = WriterArtifactRequest(
            briefing_body=briefing_body,
            selected_items=list(selected_items),
            mode=mode,
        )
        payload = await self._post_json(WRITER_BUILD_ARTIFACT_PATH, request.to_payload())
        return {str(key): str(value) for key, value in payload.items()}

    async def simulate_briefing_body(
        self,
        items: list[dict[str, Any]],
        recent_briefings: list[dict[str, Any]],
        *,
        apply_critic_rewrites: bool,
    ) -> tuple[str, dict[str, object]]:
        """Simulate one briefing body remotely."""
        request = WriterSimulationRequest(
            items=list(items),
            recent_briefings=list(recent_briefings),
            apply_critic_rewrites=apply_critic_rewrites,
        )
        payload = await self._post_json(WRITER_SIMULATE_PATH, request.to_payload())
        meta = payload.get("meta")
        return str(payload.get("markdown") or ""), dict(meta) if isinstance(meta, dict) else {}
