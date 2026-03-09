"""Composition root for local and remote BCN deployable services."""

from __future__ import annotations

from collections.abc import Callable

from bcn.common.component_settings import ServiceClientSettings
from bcn.common.component_settings import service_client_settings
from bcn.common.config import Settings
from bcn.contracts.services import AnalystWorkflow
from bcn.contracts.services import CollectorWorkflow
from bcn.contracts.services import CriticEvaluator
from bcn.contracts.services import DistributorWorkflow
from bcn.contracts.services import VerificationEvaluator
from bcn.contracts.services import WriterWorkflow
from bcn.services.analyst.service import AnalystService
from bcn.services.collector.service import CollectorService
from bcn.services.critic.service import CriticService
from bcn.services.distributor.service import DistributorService
from bcn.services.verifier.service import VerifierService
from bcn.services.writer.service import WriterService
from bcn.transports.http.analyst import RemoteAnalystClient
from bcn.transports.http.collector import RemoteCollectorClient
from bcn.transports.http.distributor import RemoteDistributorClient
from bcn.transports.http.review import RemoteCriticClient
from bcn.transports.http.review import RemoteVerifierClient
from bcn.transports.http.writer import RemoteWriterWorkflowClient

_LocalBuilder = Callable[[object], object]
_RemoteBuilder = Callable[[str, int, str], object]


def _build_remote_client(
    endpoint: ServiceClientSettings,
    remote_builder: _RemoteBuilder,
) -> object:
    """Instantiate one remote client from shared endpoint settings."""
    return remote_builder(
        base_url=endpoint.base_url,
        timeout_seconds=endpoint.timeout_seconds,
        auth_token=endpoint.auth_token,
    )


def _build_remote_or_local(
    settings: Settings,
    *,
    component: str,
    local_builder: _LocalBuilder,
    remote_builder: _RemoteBuilder,
) -> object:
    """Build one component from shared remote/local composition rules."""
    endpoint = service_client_settings(settings, component)
    if endpoint.configured:
        return _build_remote_client(endpoint, remote_builder)
    return local_builder(settings)


_LOCAL_REVIEW_BUILDERS: dict[str, _LocalBuilder] = {
    "critic": CriticService,
    "verifier": VerifierService,
}

_REMOTE_REVIEW_BUILDERS: dict[str, _RemoteBuilder] = {
    "critic": RemoteCriticClient,
    "verifier": RemoteVerifierClient,
}

_LOCAL_WORKFLOW_BUILDERS: dict[str, _LocalBuilder] = {
    "collector": CollectorService,
    "analyst": AnalystService,
    "distributor": DistributorService,
}

_REMOTE_WORKFLOW_BUILDERS: dict[str, _RemoteBuilder] = {
    "writer": RemoteWriterWorkflowClient,
    "collector": RemoteCollectorClient,
    "analyst": RemoteAnalystClient,
    "distributor": RemoteDistributorClient,
}


def build_local_critic_evaluator(settings: object) -> CriticEvaluator:
    """Build a local critic evaluator for critic-service deployments."""
    return CriticService(settings)


def build_local_verifier_evaluator(settings: object) -> VerificationEvaluator:
    """Build a local verifier evaluator for verifier-service deployments."""
    return VerifierService(settings)


def build_critic_evaluator(settings: Settings) -> CriticEvaluator:
    """Build the critic evaluator selected by configuration."""
    return _build_remote_or_local(
        settings,
        component="critic",
        local_builder=_LOCAL_REVIEW_BUILDERS["critic"],
        remote_builder=_REMOTE_REVIEW_BUILDERS["critic"],
    )


def build_verifier_evaluator(settings: Settings) -> VerificationEvaluator:
    """Build the verifier evaluator selected by configuration."""
    return _build_remote_or_local(
        settings,
        component="verifier",
        local_builder=_LOCAL_REVIEW_BUILDERS["verifier"],
        remote_builder=_REMOTE_REVIEW_BUILDERS["verifier"],
    )


def build_local_writer_workflow(settings: object) -> WriterWorkflow:
    """Build a local writer workflow with injected review evaluators."""
    critique_enabled = bool(getattr(settings, "briefing_critique_enabled", True))
    verifier_enabled = bool(getattr(settings, "briefing_verifier_enabled", True))
    critic_evaluator = (
        build_critic_evaluator(settings) if critique_enabled else None
    )
    verifier_evaluator = (
        build_verifier_evaluator(settings) if verifier_enabled else None
    )
    return WriterService(
        settings,
        critic_evaluator=critic_evaluator,
        verifier_evaluator=verifier_evaluator,
        owns_critic_evaluator=critic_evaluator is not None,
        owns_verifier_evaluator=verifier_evaluator is not None,
    )


def build_writer_workflow(settings: Settings) -> WriterWorkflow:
    """Build the writer workflow selected by configuration."""
    return _build_remote_or_local(
        settings,
        component="writer",
        local_builder=build_local_writer_workflow,
        remote_builder=_REMOTE_WORKFLOW_BUILDERS["writer"],
    )


def build_local_collector_workflow(settings: object) -> CollectorWorkflow:
    """Build a local collector workflow for collector-service deployments."""
    return CollectorService(settings)


def build_collector_workflow(settings: Settings) -> CollectorWorkflow:
    """Build the collector workflow selected by configuration."""
    return _build_remote_or_local(
        settings,
        component="collector",
        local_builder=_LOCAL_WORKFLOW_BUILDERS["collector"],
        remote_builder=_REMOTE_WORKFLOW_BUILDERS["collector"],
    )


def build_local_analyst_workflow(settings: object) -> AnalystWorkflow:
    """Build a local analyst workflow for analyst-service deployments."""
    return AnalystService(settings)


def build_analyst_workflow(settings: Settings) -> AnalystWorkflow:
    """Build the analyst workflow selected by configuration."""
    return _build_remote_or_local(
        settings,
        component="analyst",
        local_builder=_LOCAL_WORKFLOW_BUILDERS["analyst"],
        remote_builder=_REMOTE_WORKFLOW_BUILDERS["analyst"],
    )


def build_local_distributor_workflow(settings: object) -> DistributorWorkflow:
    """Build a local distributor workflow for distributor-service deployments."""
    return DistributorService(settings)


def build_distributor_workflow(settings: Settings) -> DistributorWorkflow:
    """Build the distributor workflow selected by configuration."""
    return _build_remote_or_local(
        settings,
        component="distributor",
        local_builder=_LOCAL_WORKFLOW_BUILDERS["distributor"],
        remote_builder=_REMOTE_WORKFLOW_BUILDERS["distributor"],
    )


__all__ = [
    "build_analyst_workflow",
    "build_collector_workflow",
    "build_critic_evaluator",
    "build_distributor_workflow",
    "build_local_analyst_workflow",
    "build_local_collector_workflow",
    "build_local_critic_evaluator",
    "build_local_distributor_workflow",
    "build_local_verifier_evaluator",
    "build_local_writer_workflow",
    "build_verifier_evaluator",
    "build_writer_workflow",
]
