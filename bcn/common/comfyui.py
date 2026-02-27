"""ComfyUI Flux client for cover image generation."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ComfyUIClient:
    """Async client for the ComfyUI REST API running Flux.1-schnell."""

    def __init__(
        self,
        base_url: str,
        timeout: int = 300,
        poll_interval: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._client.aclose()

    async def generate_image(
        self,
        prompt_text: str,
        filename_prefix: str = "Digest_Cover",
    ) -> str:
        """Submit a Flux workflow, poll for completion, and return the image URL.

        Args:
            prompt_text: The positive text prompt for image generation.
            filename_prefix: Prefix for the saved output filename.

        Returns:
            A ``/view?filename=...`` URL pointing to the generated image.
        """
        seed = random.randint(0, 2**53)
        workflow = self._build_workflow(prompt_text, seed, filename_prefix)

        resp = await self._client.post(
            f"{self.base_url}/prompt",
            json={"prompt": workflow},
        )
        resp.raise_for_status()
        prompt_id: str = resp.json()["prompt_id"]
        logger.info("ComfyUI prompt queued: %s", prompt_id)

        filename = await self._poll_completion(prompt_id)
        return f"{self.base_url}/view?filename={filename}"

    async def _poll_completion(self, prompt_id: str) -> str:
        """Poll ``/history/{prompt_id}`` until the image is ready.

        Args:
            prompt_id: The ComfyUI prompt identifier.

        Returns:
            The output filename of the generated image.
        """
        while True:
            await asyncio.sleep(self.poll_interval)
            resp = await self._client.get(f"{self.base_url}/history/{prompt_id}"
                                         )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()

            if prompt_id not in data:
                continue

            outputs = data[prompt_id].get("outputs", {})
            for node_output in outputs.values():
                images = node_output.get("images", [])
                if images:
                    return images[0]["filename"]

    @staticmethod
    def _build_workflow(
        prompt_text: str,
        seed: int,
        filename_prefix: str,
    ) -> dict[str, Any]:
        """Build the ComfyUI workflow payload for Flux.1-schnell.

        Uses 4 steps, cfg=1, euler sampler, simple scheduler, 1024x1024.

        Args:
            prompt_text: Positive text prompt.
            seed: Random seed for reproducibility.
            filename_prefix: Output filename prefix.

        Returns:
            A workflow dict compatible with the ComfyUI ``/prompt`` endpoint.
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
                "inputs": {
                    "ckpt_name": "flux1-schnell.safetensors"
                },
                "class_type": "CheckpointLoaderSimple",
            },
            "5": {
                "inputs": {
                    "width": 1024,
                    "height": 1024,
                    "batch_size": 1
                },
                "class_type": "EmptyLatentImage",
            },
            "6": {
                "inputs": {
                    "text": prompt_text,
                    "clip": ["4", 1]
                },
                "class_type": "CLIPTextEncode",
            },
            "7": {
                "inputs": {
                    "text": "text, watermark, blurry, low quality",
                    "clip": ["4", 1],
                },
                "class_type": "CLIPTextEncode",
            },
            "8": {
                "inputs": {
                    "samples": ["3", 0],
                    "vae": ["4", 2]
                },
                "class_type": "VAEDecode",
            },
            "9": {
                "inputs": {
                    "filename_prefix": filename_prefix,
                    "images": ["8", 0],
                },
                "class_type": "SaveImage",
            },
        }
