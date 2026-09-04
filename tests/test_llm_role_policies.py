"""Per-role request policy wiring: role timeout env vars must take effect."""

from bcn.common.config import Settings
from bcn.common.llm import LLMClient


def test_writer_timeout_override_applies():
    client = LLMClient.from_settings(
        Settings(
            llm_provider="openai_compat",
            llm_base_url="https://api.openai.com/v1",
            llm_model="gpt-5.6",
            llm_timeout=180,
            llm_timeout_writer=900,
            llm_timeout_critic=600,
        )
    )
    assert client._request_policy("writer").timeout == 900.0
    assert client._request_policy("critic").timeout == 600.0
    # Roles without an override keep the client default.
    assert client._request_policy("verifier").timeout == 180.0
    assert client._request_policy("analyst").timeout == 180.0


def test_analyst_policy_extras_still_apply():
    client = LLMClient.from_settings(
        Settings(
            llm_provider="openai_compat",
            llm_base_url="https://api.openai.com/v1",
            llm_model="gpt-5.6",
            llm_timeout_analyst=60,
            llm_chat_retries_analyst=4,
        )
    )
    policy = client._request_policy("analyst")
    assert policy.timeout == 60.0
    assert policy.chat_retries == 4
