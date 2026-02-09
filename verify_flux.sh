#!/bin/bash

# Configuration
DGX_IP="host.docker.internal"
PORT="8188"
PROMPT="A futuristic red team hacker usage data center with neon lights, high quality, 8k"

# JSON Payload (ComfyUI Workflow for Flux.1-schnell)
read -r -d '' JSON_PAYLOAD << EOM
{
  "prompt": {
    "3": {
      "inputs": {
        "seed": $((RANDOM)),
        "steps": 4,
        "cfg": 1,
        "sampler_name": "euler",
        "scheduler": "simple",
        "denoise": 1,
        "model": ["4", 0],
        "positive": ["6", 0],
        "negative": ["7", 0],
        "latent_image": ["5", 0]
      },
      "class_type": "KSampler"
    },
    "4": {
      "inputs": {
        "ckpt_name": "flux1-schnell.safetensors"
      },
      "class_type": "CheckpointLoaderSimple"
    },
    "5": {
      "inputs": {
        "width": 1024,
        "height": 1024,
        "batch_size": 1
      },
      "class_type": "EmptyLatentImage"
    },
    "6": {
      "inputs": {
        "text": "$PROMPT",
        "clip": ["4", 1]
      },
      "class_type": "CLIPTextEncode"
    },
    "7": {
      "inputs": {
        "text": "text, watermark, blurry, low quality",
        "clip": ["4", 1]
      },
      "class_type": "CLIPTextEncode"
    },
    "8": {
      "inputs": {
        "samples": ["3", 0],
        "vae": ["4", 2]
      },
      "class_type": "VAEDecode"
    },
    "9": {
      "inputs": {
        "filename_prefix": "Flux_Test",
        "images": ["8", 0]
      },
      "class_type": "SaveImage"
    }
  }
}
EOM

echo "Sending request to http://$DGX_IP:$PORT/prompt..."
curl -X POST http://$DGX_IP:$PORT/prompt \
  -H "Content-Type: application/json" \
  -d "$JSON_PAYLOAD"

echo ""
echo "---------------------------------------------------"
echo "If successful, check the ComfyUI output directory."
echo "You can view the image at (filename may vary):"
echo "http://$DGX_IP:$PORT/view?filename=Flux_Test_00001_.png"
