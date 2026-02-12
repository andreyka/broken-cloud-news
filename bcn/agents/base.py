from __future__ import annotations

import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill


def build_agent_card(
    name: str,
    description: str,
    url: str,
    skills: list[AgentSkill],
) -> AgentCard:
    return AgentCard(
        name=name,
        description=description,
        url=url,
        version="2.0.0",
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(),
        skills=skills,
    )


def make_app(agent_card: AgentCard, executor) -> A2AStarletteApplication:
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )
    return A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=handler,
    )


async def serve_agent(agent_card: AgentCard, executor, port: int) -> None:
    """Run an A2A agent as an async uvicorn server (non-blocking)."""
    app_builder = make_app(agent_card, executor)
    config = uvicorn.Config(
        app_builder.build(),
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()
