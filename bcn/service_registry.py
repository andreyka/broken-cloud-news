"""Composition root for local and remote BCN deployable services."""

from __future__ import annotations

from bcn.services.critic.service import CriticService
from bcn.services.verifier.service import VerifierService
from bcn.services.writer.service import WriterService
from bcn.common.config import Settings
from bcn.contracts.services import CriticEvaluator
from bcn.contracts.services import VerificationEvaluator
from bcn.contracts.services import WriterWorkflow
from bcn.transports.http.review import RemoteCriticClient
from bcn.transports.http.review import RemoteVerifierClient
from bcn.transports.http.writer import RemoteWriterWorkflowClient


def build_critic_evaluator(settings: Settings) -> CriticEvaluator:
    """Build the critic evaluator selected by configuration."""
    if settings.critic_service_url:
        return RemoteCriticClient(
            base_url=settings.critic_service_url,
            timeout_seconds=settings.service_request_timeout_seconds,
            auth_token=settings.service_auth_token,
        )
    return CriticService(settings)


def build_verifier_evaluator(settings: Settings) -> VerificationEvaluator:
    """Build the verifier evaluator selected by configuration."""
    if settings.verifier_service_url:
        return RemoteVerifierClient(
            base_url=settings.verifier_service_url,
            timeout_seconds=settings.service_request_timeout_seconds,
            auth_token=settings.service_auth_token,
        )
    return VerifierService(settings)


def build_local_writer_workflow(settings: Settings) -> WriterWorkflow:
    """Build a local writer workflow while still honoring remote review services."""
    if (
        settings.critic_service_url
        or settings.verifier_service_url
    ):
        critic_evaluator = build_critic_evaluator(settings)
        verifier_evaluator = build_verifier_evaluator(settings)
        return WriterService(
            settings,
            critic_evaluator=critic_evaluator,
            verifier_evaluator=verifier_evaluator,
            owns_critic_evaluator=True,
            owns_verifier_evaluator=True,
        )
    return WriterService(settings)


def build_writer_workflow(settings: Settings) -> WriterWorkflow:
    """Build the writer workflow selected by configuration."""
    if settings.writer_service_url:
        return RemoteWriterWorkflowClient(
            base_url=settings.writer_service_url,
            timeout_seconds=settings.service_request_timeout_seconds,
            auth_token=settings.service_auth_token,
        )
    return build_local_writer_workflow(settings)


def build_local_critic_evaluator(settings: Settings) -> CriticEvaluator:
    """Build a local critic evaluator for critic-service deployments."""
    return CriticService(settings)


def build_local_verifier_evaluator(settings: Settings) -> VerificationEvaluator:
    """Build a local verifier evaluator for verifier-service deployments."""
    return VerifierService(settings)
