"""Shared A2A agent infrastructure (card builder, server launcher)."""

from __future__ import annotations

import inspect
from typing import Any

from a2a.server.agent_execution import AgentExecutor
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities
from a2a.types import AgentCard
from a2a.types import AgentSkill
import uvicorn


def build_agent_card(
    name: str,
    description: str,
    url: str,
    skills: list[AgentSkill],
) -> AgentCard:
    """Create an A2A agent card with default capabilities.

    Args:
        name: Human-readable agent name.
        description: One-line description of the agent's purpose.
        url: Base URL where the agent is reachable.
        skills: List of skills the agent advertises.

    Returns:
        A fully populated ``AgentCard``.
    """
    return AgentCard(
        name=name,
        description=description,
        url=url,
        version="2.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(),
        skills=skills,
    )


def make_app(
    agent_card: AgentCard,
    executor: AgentExecutor,
) -> A2AStarletteApplication:
    """Wire an agent executor into a Starlette ASGI application.

    Args:
        agent_card: The card describing this agent.
        executor: The executor that handles incoming messages.

    Returns:
        A configured ``A2AStarletteApplication``.
    """
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )
    return A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=handler,
    )


async def serve_agent(
    agent_card: AgentCard,
    executor: AgentExecutor,
    port: int,
) -> None:
    """Run an A2A agent as an async uvicorn server.

    Args:
        agent_card: The card describing this agent.
        executor: The executor that handles incoming messages.
        port: TCP port to bind.
    """
    app_builder = make_app(agent_card, executor)
    config = uvicorn.Config(
        app_builder.build(),
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def enqueue_event_safe(event_queue: EventQueue, event: Any) -> None:
    """Enqueue an event for both sync and async queue implementations."""
    result = event_queue.enqueue_event(event)
    if inspect.isawaitable(result):
        await result
