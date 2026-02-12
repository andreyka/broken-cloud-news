from __future__ import annotations

import asyncio
import logging
import random

import httpx

logger = logging.getLogger(__name__)


class ComfyUIClient:
    def __init__(self, base_url: str, timeout: int = 300, poll_interval: int = 2):
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def generate_image(self, prompt_text: str, filename_prefix: str = "Digest_Cover") -> str:
        """Submit ComfyUI Flux workflow, poll for completion, return image URL."""
        seed = random.randint(0, 2**53)
        workflow = self._build_workflow(prompt_text, seed, filename_prefix)

        resp = await self._client.post(
            f"{self.base_url}/prompt",
            json={"prompt": workflow},
        )
        resp.raise_for_status()
        prompt_id = resp.json()["prompt_id"]
        logger.info("ComfyUI prompt queued: %s", prompt_id)

        filename = await self._poll_completion(prompt_id)
        return f"{self.base_url}/view?filename={filename}"

    async def _poll_completion(self, prompt_id: str) -> str:
        """Poll /history/{prompt_id} until the image is ready."""
        while True:
            await asyncio.sleep(self.poll_interval)
            resp = await self._client.get(f"{self.base_url}/history/{prompt_id}")
            resp.raise_for_status()
            data = resp.json()

            if prompt_id not in data:
                continue

            outputs = data[prompt_id].get("outputs", {})
            for node_id, node_output in outputs.items():
                images = node_output.get("images", [])
                if images:
                    return images[0]["filename"]

        # Unreachable, but type checker wants it
        raise RuntimeError("ComfyUI polling exited unexpectedly")

    @staticmethod
    def _build_workflow(prompt_text: str, seed: int, filename_prefix: str) -> dict:
        """Build the exact ComfyUI workflow from verify_flux.sh / digest_generator.json.

        Flux.1-schnell: 4 steps, cfg=1, euler sampler, simple scheduler, 1024x1024.
        """
        return {
            "3": {
                "inputs": {
                    "seed": seed,
                    "steps": 4,
                    "cfg": 1,
                    "sampler_name": "euler",
                    "scheduler": "simple",
                    "denoise": 1,
                    "model": ["4", 0],
                    "positive": ["6", 0],
                    "negative": ["7", 0],
                    "latent_image": ["5", 0],
                },
                "class_type": "KSampler",
            },
            "4": {
                "inputs": {"ckpt_name": "flux1-schnell.safetensors"},
                "class_type": "CheckpointLoaderSimple",
            },
            "5": {
                "inputs": {"width": 1024, "height": 1024, "batch_size": 1},
                "class_type": "EmptyLatentImage",
            },
            "6": {
                "inputs": {"text": prompt_text, "clip": ["4", 1]},
                "class_type": "CLIPTextEncode",
            },
            "7": {
                "inputs": {"text": "text, watermark, blurry, low quality", "clip": ["4", 1]},
                "class_type": "CLIPTextEncode",
            },
            "8": {
                "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
                "class_type": "VAEDecode",
            },
            "9": {
                "inputs": {"filename_prefix": filename_prefix, "images": ["8", 0]},
                "class_type": "SaveImage",
            },
        }
