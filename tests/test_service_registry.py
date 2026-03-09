from __future__ import annotations

import pytest

from bcn.common.models import AnalyzedItemUpdate
from bcn.common.models import CollectedNewsItem
from bcn.services.writer.service import WriterService
from bcn.common.config import Settings
from bcn.service_registry import build_analyst_workflow
from bcn.service_registry import build_collector_workflow
from bcn.service_registry import build_critic_evaluator
from bcn.service_registry import build_local_writer_workflow
from bcn.service_registry import build_verifier_evaluator
from bcn.service_registry import build_writer_workflow
from bcn.transports.http.analyst import RemoteAnalystClient
from bcn.transports.http.collector import RemoteCollectorClient
from bcn.transports.http.review import RemoteCriticClient
from bcn.transports.http.review import RemoteVerifierClient
from bcn.transports.http.writer import RemoteWriterWorkflowClient


def _make_settings(**overrides) -> Settings:
    defaults = {
        "database_url": "postgresql://test:test@localhost:5432/test",
        "llm_base_url": "http://fake-llm:8000/v1",
        "llm_model": "test-model",
        "comfyui_url": "http://fake-comfy:8188",
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_build_writer_workflow_returns_remote_client_when_url_is_configured():
    workflow = build_writer_workflow(
        _make_settings(writer_service_url="http://writer.internal:8081")
    )
    try:
        assert isinstance(workflow, RemoteWriterWorkflowClient)
    finally:
        await workflow.close()


@pytest.mark.asyncio
async def test_build_review_evaluators_return_remote_clients_when_configured():
    settings = _make_settings(
        critic_service_url="http://critic.internal:8082",
        verifier_service_url="http://verifier.internal:8083",
    )
    critic = build_critic_evaluator(settings)
    verifier = build_verifier_evaluator(settings)
    try:
        assert isinstance(critic, RemoteCriticClient)
        assert isinstance(verifier, RemoteVerifierClient)
    finally:
        await critic.close()
        await verifier.close()


@pytest.mark.asyncio
async def test_build_local_writer_workflow_injects_remote_review_clients():
    settings = _make_settings(
        critic_service_url="http://critic.internal:8082",
        verifier_service_url="http://verifier.internal:8083",
    )
    workflow = build_local_writer_workflow(settings)
    assert isinstance(workflow, WriterService)
    assert isinstance(workflow.critic_evaluator, RemoteCriticClient)
    assert isinstance(workflow.verifier_evaluator, RemoteVerifierClient)
    await workflow.close()


@pytest.mark.asyncio
async def test_build_collector_and_analyst_workflows_return_remote_clients_when_configured():
    settings = _make_settings(
        collector_service_url="http://collector.internal:8084",
        analyst_service_url="http://analyst.internal:8085",
    )
    collector = build_collector_workflow(settings)
    analyst = build_analyst_workflow(settings)
    try:
        assert isinstance(collector, RemoteCollectorClient)
        assert isinstance(analyst, RemoteAnalystClient)
    finally:
        await collector.close()
        await analyst.close()
