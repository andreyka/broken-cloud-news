import os
import sys
import json
from unittest.mock import AsyncMock

import pytest

from bcn.services.analyst.llm import AnalystLLM
from bcn.common.llm import LLMClient

sys.path.insert(0, os.path.abspath("."))

@pytest.mark.asyncio
async def test_analyst_canonical_url_extraction():
    client = LLMClient(base_url="http://fake-llm:8000/v1", model="test-model", timeout=5)
    client.chat_for_role = AsyncMock(
        return_value=json.dumps(
            {
                "summary": "Critical Claude Code vuln",
                "relevance_score": 8,
                "tags": ["rce", "developer-tools"],
                "image_prompt": "secure coding workstation under attack",
                "canonical_url": "https://research.checkpoint.com/2026/rce-and-api-token-exfiltration-through-claude-code-project-files-cve-2025-59536/",
            }
        )
    )
    analyst = AnalystLLM(client)

    title = "Claude Code flaws exposed developer devices to silent hacking - @Fox0x01 @steipete"
    url = "https://x.com/Dinosn/status/2027220981203218535"
    content = """
    A critical vulnerability in Claude Code exposed developer devices to RCE and API token exfiltration. 
    Reference links:
    - https://www.securityweek.com/claude-code-flaws-exposed-developer-devices-to-silent-hacking/
    
    --- Scraped content from https://www.securityweek.com/claude-code-flaws-exposed-developer-devices-to-silent-hacking/ ---
    Claude Code Flaws Exposed Developer Devices to Silent Hacking ...
    Vulnerabilities in Anthropic’s new Claude Code coding agent could be exploited by malicious threat actors to compromise developer systems.
    The flaws, discovered by researchers at Check Point, allowed a remote attacker to gain access to files and execute arbitrary code on the victim’s machine.
    Check Point Research details the vulnerabilities in their advisory: https://research.checkpoint.com/2026/rce-and-api-token-exfiltration-through-claude-code-project-files-cve-2025-59536/
    Anthropic has released a patch and developers are urged to update immediately.
    """

    result = await analyst.analyze_item(title=title, content=content, url=url)

    assert (
        result.canonical_url
        == "https://research.checkpoint.com/2026/rce-and-api-token-exfiltration-through-claude-code-project-files-cve-2025-59536/"
    )
    assert result.relevance_score >= 7
    client.chat_for_role.assert_awaited_once()
