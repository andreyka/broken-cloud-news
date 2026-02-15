from __future__ import annotations

import httpx
import pytest
import respx

from bcn.comfyui import ComfyUIClient


@pytest.fixture
def comfyui():
    return ComfyUIClient(base_url="http://fake-comfy:8188", timeout=5, poll_interval=0)


class TestComfyUIClient:
    @respx.mock
    @pytest.mark.asyncio
    async def test_generate_image(self, comfyui):
        prompt_id = "abc123"
        respx.post("http://fake-comfy:8188/prompt").mock(
            return_value=httpx.Response(200, json={"prompt_id": prompt_id})
        )
        respx.get(f"http://fake-comfy:8188/history/{prompt_id}").mock(
            return_value=httpx.Response(200, json={
                prompt_id: {
                    "outputs": {
                        "9": {"images": [{"filename": "cover_00001_.png"}]}
                    }
                }
            })
        )

        url = await comfyui.generate_image("cyberpunk", "Cover")
        assert url == "http://fake-comfy:8188/view?filename=cover_00001_.png"

    @respx.mock
    @pytest.mark.asyncio
    async def test_polls_until_ready(self, comfyui):
        prompt_id = "xyz"
        respx.post("http://fake-comfy:8188/prompt").mock(
            return_value=httpx.Response(200, json={"prompt_id": prompt_id})
        )
        history_route = respx.get(f"http://fake-comfy:8188/history/{prompt_id}")
        history_route.side_effect = [
            # First poll: not ready yet
            httpx.Response(200, json={}),
            # Second poll: ready
            httpx.Response(200, json={
                prompt_id: {
                    "outputs": {"9": {"images": [{"filename": "done.png"}]}}
                }
            }),
        ]

        url = await comfyui.generate_image("prompt", "Prefix")
        assert "done.png" in url
        assert history_route.call_count == 2

    def test_build_workflow(self):
        wf = ComfyUIClient._build_workflow("test prompt", seed=42, filename_prefix="Test")
        assert wf["6"]["inputs"]["text"] == "test prompt"
        assert wf["3"]["inputs"]["seed"] == 42
        assert wf["9"]["inputs"]["filename_prefix"] == "Test"
        assert wf["3"]["inputs"]["steps"] == 4
        assert wf["5"]["inputs"]["width"] == 1024
