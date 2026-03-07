"""Typed BCN agent clients with pluggable transports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from typing import Protocol
from uuid import UUID

from bcn.common.agent_runtime import run_agent_directly
from bcn.common.agent_runtime import send_to_agent
from bcn.common.config import Settings

AgentName = Literal[
    "collector",
    "analyst",
    "writer",
    "distributor",
    "critic",
    "verifier",
]


class DirectAgentRunner(Protocol):
    """Protocol for in-process agent execution helpers."""

    async def __call__(
        self,
        executor_cls: type,
        settings: Settings,
        skill: str,
    ) -> str:
        """Run one agent executor directly and return its text output."""


class A2ASender(Protocol):
    """Protocol for A2A message send helpers."""

    async def __call__(
        self,
        target: str | int,
        skill: str,
        *,
        timeout_seconds: int = 180,
    ) -> str:
        """Send one skill message to an A2A target and return its text output."""


class PortSkillSender(Protocol):
    """Protocol for legacy scheduler sender callables."""

    async def __call__(self, port: int, skill: str) -> str:
        """Send one skill message to a localhost port and return text output."""


class AgentClient(Protocol):
    """Typed interface used by workflow and CLI orchestration."""

    async def call_collector(self, skill: str) -> str:
        """Invoke the collector agent with a raw skill payload."""

    async def call_analyst(self, skill: str) -> str:
        """Invoke the analyst agent with a raw skill payload."""

    async def call_writer(self, skill: str) -> str:
        """Invoke the writer agent with a raw skill payload."""

    async def call_distributor(self, skill: str) -> str:
        """Invoke the distributor agent with a raw skill payload."""

    async def call_critic(self, skill: str) -> str:
        """Invoke the critic agent with a raw skill payload."""

    async def call_verifier(self, skill: str) -> str:
        """Invoke the verifier agent with a raw skill payload."""

    async def collect_ghsa(self) -> str:
        """Run GHSA collection."""

    async def collect_rss(self) -> str:
        """Run RSS collection."""

    async def collect_twitter(self) -> str:
        """Run Twitter/X collection."""

    async def collect_reddit(self) -> str:
        """Run Reddit collection."""

    async def collect_all(self) -> str:
        """Run all collectors concurrently."""

    async def analyze_new_items(self) -> str:
        """Analyze newly collected items."""

    async def generate_briefing(self, mode: str) -> str:
        """Generate one briefing for the requested workflow mode."""

    async def distribute_briefing(
        self,
        *,
        mode: str,
        briefing_id: UUID | None = None,
    ) -> str:
        """Distribute the latest or requested briefing."""

    async def critique_latest(self) -> str:
        """Critique the latest stored briefing."""

    async def critique_markdown(self, markdown: str) -> str:
        """Critique explicit markdown text."""

    async def verify_latest(self) -> str:
        """Verify the latest stored briefing."""

    async def verify_markdown(self, markdown: str) -> str:
        """Verify explicit markdown text."""


def _executor_for_agent(agent_name: AgentName) -> type:
    """Return the executor class for a named agent."""
    if agent_name == "collector":
        from bcn.agents.collector.agent import CollectorExecutor

        return CollectorExecutor
    if agent_name == "analyst":
        from bcn.agents.analyst.agent import AnalystExecutor

        return AnalystExecutor
    if agent_name == "writer":
        from bcn.agents.writer.agent import WriterExecutor

        return WriterExecutor
    if agent_name == "distributor":
        from bcn.agents.distributor.agent import DistributorExecutor

        return DistributorExecutor
    if agent_name == "critic":
        from bcn.agents.critic.agent import CriticExecutor

        return CriticExecutor
    if agent_name == "verifier":
        from bcn.agents.verifier.agent import VerifierExecutor

        return VerifierExecutor
    raise ValueError(f"Unknown agent name: {agent_name}")


class _AgentClientMixin:
    """Shared typed skill helpers used by concrete agent clients."""

    async def _call_agent(self, agent_name: AgentName, skill: str) -> str:
        raise NotImplementedError

    async def call_collector(self, skill: str) -> str:
        return await self._call_agent("collector", skill)

    async def call_analyst(self, skill: str) -> str:
        return await self._call_agent("analyst", skill)

    async def call_writer(self, skill: str) -> str:
        return await self._call_agent("writer", skill)

    async def call_distributor(self, skill: str) -> str:
        return await self._call_agent("distributor", skill)

    async def call_critic(self, skill: str) -> str:
        return await self._call_agent("critic", skill)

    async def call_verifier(self, skill: str) -> str:
        return await self._call_agent("verifier", skill)

    async def collect_ghsa(self) -> str:
        return await self.call_collector("collect_ghsa")

    async def collect_rss(self) -> str:
        return await self.call_collector("collect_rss")

    async def collect_twitter(self) -> str:
        return await self.call_collector("collect_twitter")

    async def collect_reddit(self) -> str:
        return await self.call_collector("collect_reddit")

    async def collect_all(self) -> str:
        return await self.call_collector("collect_all")

    async def analyze_new_items(self) -> str:
        return await self.call_analyst("analyze_new_items")

    async def generate_briefing(self, mode: str) -> str:
        return await self.call_writer(f"generate_briefing::{mode}")

    async def distribute_briefing(
        self,
        *,
        mode: str,
        briefing_id: UUID | None = None,
    ) -> str:
        if briefing_id is None:
            return await self.call_distributor(f"distribute_briefing::{mode}")
        return await self.call_distributor(
            f"distribute_briefing::{briefing_id}::{mode}"
        )

    async def critique_latest(self) -> str:
        return await self.call_critic("critique_latest")

    async def critique_markdown(self, markdown: str) -> str:
        return await self.call_critic(f"critique_markdown::{markdown}")

    async def verify_latest(self) -> str:
        return await self.call_verifier("verify_latest")

    async def verify_markdown(self, markdown: str) -> str:
        return await self.call_verifier(f"verify_markdown::{markdown}")


@dataclass(frozen=True)
class DirectAgentClient(_AgentClientMixin):
    """In-process agent client used for local CLI calls and tests."""

    settings: Settings
    runner: DirectAgentRunner = run_agent_directly

    async def _call_agent(self, agent_name: AgentName, skill: str) -> str:
        executor_cls = _executor_for_agent(agent_name)
        return await self.runner(
            executor_cls=executor_cls,
            settings=self.settings,
            skill=skill,
        )


@dataclass(frozen=True)
class A2AAgentClient(_AgentClientMixin):
    """A2A agent client that resolves per-agent endpoint URLs from settings."""

    settings: Settings
    timeout_seconds: int = 180
    sender: A2ASender = send_to_agent

    async def _call_agent(self, agent_name: AgentName, skill: str) -> str:
        return await self.sender(
            self.settings.agent_url(agent_name),
            skill,
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True)
class PortSenderAgentClient(_AgentClientMixin):
    """Adapter that preserves the legacy sender(port, skill) runtime shape."""

    settings: Settings
    sender: PortSkillSender

    async def _call_agent(self, agent_name: AgentName, skill: str) -> str:
        return await self.sender(self.settings.agent_port(agent_name), skill)


def build_direct_agent_client(
    settings: Settings,
    *,
    runner: DirectAgentRunner = run_agent_directly,
) -> AgentClient:
    """Build an in-process agent client."""
    return DirectAgentClient(settings=settings, runner=runner)


def build_a2a_agent_client(
    settings: Settings,
    *,
    timeout_seconds: int | None = None,
    sender: A2ASender = send_to_agent,
) -> AgentClient:
    """Build an endpoint-based A2A agent client."""
    return A2AAgentClient(
        settings=settings,
        timeout_seconds=(
            int(timeout_seconds)
            if timeout_seconds is not None
            else int(settings.a2a_request_timeout_seconds)
        ),
        sender=sender,
    )


def build_port_sender_agent_client(
    settings: Settings,
    *,
    sender: PortSkillSender,
) -> AgentClient:
    """Build a typed client around the legacy sender(port, skill) contract."""
    return PortSenderAgentClient(settings=settings, sender=sender)


def build_default_agent_client(
    settings: Settings,
    *,
    direct_runner: DirectAgentRunner = run_agent_directly,
    a2a_sender: A2ASender = send_to_agent,
    timeout_seconds: int | None = None,
) -> AgentClient:
    """Build the default client for the current settings profile.

    If any agent endpoint URL is configured explicitly, orchestration uses A2A.
    Otherwise it keeps the current in-process execution path for local CLI usage.
    """
    if settings.has_agent_url_overrides():
        return build_a2a_agent_client(
            settings,
            timeout_seconds=timeout_seconds,
            sender=a2a_sender,
        )
    return build_direct_agent_client(settings, runner=direct_runner)


__all__ = [
    "A2AAgentClient",
    "AgentClient",
    "AgentName",
    "DirectAgentClient",
    "PortSenderAgentClient",
    "build_a2a_agent_client",
    "build_default_agent_client",
    "build_direct_agent_client",
    "build_port_sender_agent_client",
]
