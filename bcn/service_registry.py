"""Composition root for local and remote BCN deployable services."""

from __future__ import annotations

from bcn.common.component_settings import service_client_settings
from bcn.services.critic.service import CriticService
from bcn.services.analyst.service import AnalystService
from bcn.services.collector.service import CollectorService
from bcn.services.distributor.service import DistributorService
from bcn.services.verifier.service import VerifierService
from bcn.services.writer.service import WriterService
from bcn.common.config import Settings
from bcn.contracts.services import AnalystWorkflow
from bcn.contracts.services import CollectorWorkflow
from bcn.contracts.services import CriticEvaluator
from bcn.contracts.services import DistributorWorkflow
from bcn.contracts.services import VerificationEvaluator
from bcn.contracts.services import WriterWorkflow
from bcn.transports.http.analyst import RemoteAnalystClient
from bcn.transports.http.collector import RemoteCollectorClient
from bcn.transports.http.distributor import RemoteDistributorClient
from bcn.transports.http.review import RemoteCriticClient
from bcn.transports.http.review import RemoteVerifierClient
from bcn.transports.http.writer import RemoteWriterWorkflowClient


def build_critic_evaluator(settings: Settings) -> CriticEvaluator:
    """Build the critic evaluator selected by configuration."""
    endpoint = service_client_settings(settings, "critic")
    if endpoint.configured:
        return RemoteCriticClient(
            base_url=endpoint.base_url,
            timeout_seconds=endpoint.timeout_seconds,
            auth_token=endpoint.auth_token,
        )
    return CriticService(settings)


def build_verifier_evaluator(settings: Settings) -> VerificationEvaluator:
    """Build the verifier evaluator selected by configuration."""
    endpoint = service_client_settings(settings, "verifier")
    if endpoint.configured:
        return RemoteVerifierClient(
            base_url=endpoint.base_url,
            timeout_seconds=endpoint.timeout_seconds,
            auth_token=endpoint.auth_token,
        )
    return VerifierService(settings)


def build_local_writer_workflow(settings: Settings) -> WriterWorkflow:
    """Build a local writer workflow with injected review evaluators."""
    critique_enabled = bool(getattr(settings, "briefing_critique_enabled", True))
    verifier_enabled = bool(getattr(settings, "briefing_verifier_enabled", True))
    critic_evaluator = build_critic_evaluator(settings) if critique_enabled else None
    verifier_evaluator = build_verifier_evaluator(settings) if verifier_enabled else None
    return WriterService(
        settings,
        critic_evaluator=critic_evaluator,
        verifier_evaluator=verifier_evaluator,
        owns_critic_evaluator=critic_evaluator is not None,
        owns_verifier_evaluator=verifier_evaluator is not None,
    )


def build_writer_workflow(settings: Settings) -> WriterWorkflow:
    """Build the writer workflow selected by configuration."""
    endpoint = service_client_settings(settings, "writer")
    if endpoint.configured:
        return RemoteWriterWorkflowClient(
            base_url=endpoint.base_url,
            timeout_seconds=endpoint.timeout_seconds,
            auth_token=endpoint.auth_token,
        )
    return build_local_writer_workflow(settings)


def build_collector_workflow(settings: Settings) -> CollectorWorkflow:
    """Build the collector workflow selected by configuration."""
    endpoint = service_client_settings(settings, "collector")
    if endpoint.configured:
        return RemoteCollectorClient(
            base_url=endpoint.base_url,
            timeout_seconds=endpoint.timeout_seconds,
            auth_token=endpoint.auth_token,
        )
    return CollectorService(settings)


def build_analyst_workflow(settings: Settings) -> AnalystWorkflow:
    """Build the analyst workflow selected by configuration."""
    endpoint = service_client_settings(settings, "analyst")
    if endpoint.configured:
        return RemoteAnalystClient(
            base_url=endpoint.base_url,
            timeout_seconds=endpoint.timeout_seconds,
            auth_token=endpoint.auth_token,
        )
    return AnalystService(settings)


def build_distributor_workflow(settings: Settings) -> DistributorWorkflow:
    """Build the distributor workflow selected by configuration."""
    endpoint = service_client_settings(settings, "distributor")
    if endpoint.configured:
        return RemoteDistributorClient(
            base_url=endpoint.base_url,
            timeout_seconds=endpoint.timeout_seconds,
            auth_token=endpoint.auth_token,
        )
    return DistributorService(settings)


def build_local_critic_evaluator(settings: Settings) -> CriticEvaluator:
    """Build a local critic evaluator for critic-service deployments."""
    return CriticService(settings)


def build_local_verifier_evaluator(settings: Settings) -> VerificationEvaluator:
    """Build a local verifier evaluator for verifier-service deployments."""
    return VerifierService(settings)


def build_local_collector_workflow(settings: Settings) -> CollectorWorkflow:
    """Build a local collector workflow for collector-service deployments."""
    return CollectorService(settings)


def build_local_analyst_workflow(settings: Settings) -> AnalystWorkflow:
    """Build a local analyst workflow for analyst-service deployments."""
    return AnalystService(settings)


def build_local_distributor_workflow(settings: Settings) -> DistributorWorkflow:
    """Build a local distributor workflow for distributor-service deployments."""
    return DistributorService(settings)
