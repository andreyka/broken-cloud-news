"""Shadow candidate endpoint probe: only override-introduced URLs are probed."""

import pytest

from bcn.common.config import Settings
from bcn.workflows.automation import _shadow_candidate_endpoint_error


@pytest.mark.asyncio
async def test_probe_skips_champion_urls(monkeypatch):
    champion = Settings(
        llm_provider="openai_compat",
        llm_base_url="https://api.openai.com/v1",
    )
    candidate = Settings(
        llm_provider="openai_compat",
        llm_base_url="https://api.openai.com/v1",
        llm_provider_writer="openai_compat",
        llm_base_url_writer="http://model_bridge:8000/v1",
    )
    probed: list[str] = []

    async def fake_probe(base_url: str):
        probed.append(base_url)
        return None

    monkeypatch.setattr(
        "bcn.workflows.automation._probe_openai_compat_endpoint", fake_probe
    )

    error = await _shadow_candidate_endpoint_error(candidate, champion)

    assert error is None
    # Critic/verifier inherit the champion base URL and must not be probed;
    # only the bridge URL the override introduced gets checked.
    assert probed == ["http://model_bridge:8000/v1"]


@pytest.mark.asyncio
async def test_probe_reports_unreachable_override_endpoint(monkeypatch):
    champion = Settings(
        llm_provider="openai_compat",
        llm_base_url="https://api.openai.com/v1",
    )
    candidate = Settings(
        llm_provider="openai_compat",
        llm_base_url="https://api.openai.com/v1",
        llm_provider_writer="openai_compat",
        llm_base_url_writer="http://model_bridge:8000/v1",
    )

    async def fake_probe(base_url: str):
        return f"{base_url}/models returned 502"

    monkeypatch.setattr(
        "bcn.workflows.automation._probe_openai_compat_endpoint", fake_probe
    )

    error = await _shadow_candidate_endpoint_error(candidate, champion)

    assert error == "http://model_bridge:8000/v1/models returned 502"
