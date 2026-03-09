"""Shared helpers for BCN CLI command modules."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable

from bcn.common.component_settings import load_component_service_settings
from bcn.common.config import Settings


def build_settings() -> Settings:
    """Construct a fresh settings object for one CLI invocation."""
    return Settings()


def build_component_settings(component: str) -> object:
    """Construct component-scoped settings for one service deployment."""
    return load_component_service_settings(component)


def run_async(factory: Callable[[], Awaitable[None]]) -> None:
    """Execute one async CLI action in a fresh event loop."""
    asyncio.run(factory())


__all__ = [
    "build_component_settings",
    "build_settings",
    "run_async",
]
