# Broken Cloud News Agent v2

A Python-based cloud security briefing agent using the **Google A2A (Agent-to-Agent) protocol**. Collects, analyzes, and distributes security news via local AI inference on **DGX Spark** (Qwen LLM + ComfyUI Flux).

## Architecture

Four A2A agents communicate via JSON-RPC, each running as an HTTP server:

| Agent | Port | Role |
|-------|------|------|
| **Collector** | 9001 | GHSA, Twitter/X (X API), RSS (CISA + AWS) |
| **Analyst** | 9002 | LLM scoring & summarization via Qwen |
| **Writer** | 9003 | Briefing generation + Flux cover image |
| **Distributor** | 9004 | Telegram, Email, Slack publishing |

```
GitHub/RSS/Twitter --> Collector --> PostgreSQL --> Analyst --> Writer --> Distributor
                                     (news_items)   (Qwen LLM)  (Flux)   (TG/Email/Slack)
```

---

## Quick Start

### 1. Prerequisites
- Python 3.12+
- Docker & Docker Compose (for PostgreSQL + Browserless)
- DGX Spark with Qwen + ComfyUI deployed (see AI Infrastructure below)

### 2. Setup
```bash
git clone https://github.com/broken-cloud-news/broken-cloud-news.git
cd broken-cloud-news

# Start PostgreSQL
docker-compose up -d postgres

# Install Python package
pip install -e .

# Configure
cp .env.example .env
# Edit .env with your tokens and endpoints
```

### 3. Run

**Single commands:**
```bash
bcn collect              # Collect from all sources
bcn collect -s ghsa      # Collect GHSA only
bcn analyze              # Analyze new items with LLM
bcn write                # Generate briefing + cover image
bcn distribute           # Send to configured channels
bcn pipeline             # Full pipeline (collect -> analyze -> write -> distribute)
```

**Daemon mode** (all agents + scheduler):
```bash
bcn run
```

**Docker (full stack):**
```bash
docker-compose up -d
```

---

## AI Infrastructure (DGX Node)

These components run on your NVIDIA DGX (or heavy GPU server) to provide offline inference.

### Prerequisites
*   NVIDIA Drivers & CUDA 12.x
*   NVIDIA Container Toolkit installed and configured for Docker.

### 1. Deploy Qwen3-VL (Visual Understanding)
Runs as an OpenAI-compatible API using **vLLM**.

```bash
docker run --rm -it \
    --gpus all \
    --ipc=host \
    --ulimit memlock=-1 --ulimit stack=67108864 \
    --shm-size=32g \
    -p 8000:8000 \
    -e HF_HOME=/root/.cache/huggingface \
    -v ~/.cache/huggingface:/root/.cache/huggingface \
    nvcr.io/nvidia/vllm:25.12.post1-py3 \
    vllm serve Qwen/Qwen3-VL-30B-A3B-Instruct-FP8 \
    --host 0.0.0.0 --port 8000 \
    --trust-remote-code \
    --dtype auto \
    --gpu-memory-utilization 0.55 \
    --max-model-len 16384 \
    --limit-mm-per-prompt '{"image":2,"video":0}'
```

*   **Endpoint**: `http://<DGX_IP>:8000/v1`

### 2. Deploy Flux.1-schnell (Image Generation)
Runs **ComfyUI** with a custom-built Docker image.

#### A. Build Custom Image
```bash
cat <<EOF > Dockerfile.comfyui
FROM nvcr.io/nvidia/pytorch:25.09-py3
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends git libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /opt
RUN git clone https://github.com/comfyanonymous/ComfyUI.git
WORKDIR /opt/ComfyUI
RUN python -m pip install --upgrade pip
RUN pip install torchsde
RUN sed -i '/^torch/d;/^torchvision/d;/^torchaudio/d' requirements.txt
RUN pip install -r requirements.txt
EXPOSE 8188
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188"]
EOF

docker build -t comfyui:arm64-cuda -f Dockerfile.comfyui .
```

#### B. Prepare Models
```bash
mkdir -p ~/comfyui/models/checkpoints ~/comfyui/input ~/comfyui/output

wget -O ~/comfyui/models/checkpoints/flux1-schnell.safetensors \
  https://huggingface.co/black-forest-labs/FLUX.1-schnell/resolve/main/flux1-schnell.safetensors
```

#### C. Run Flux
```bash
docker run --rm -it \
  --gpus all \
  --ipc=host \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  --shm-size=32g \
  -p 8188:8188 \
  -v ~/comfyui/models:/opt/ComfyUI/models \
  -v ~/comfyui/input:/opt/ComfyUI/input \
  -v ~/comfyui/output:/opt/ComfyUI/output \
  comfyui:arm64-cuda
```

*   **Endpoint**: `http://<DGX_IP>:8188`

---

## Configuration

All settings via environment variables with `BCN_` prefix. See `.env.example` for the full list.

Key settings:
| Variable | Default | Description |
|----------|---------|-------------|
| `BCN_LLM_BASE_URL` | `http://host.docker.internal:8000/v1` | Qwen API endpoint |
| `BCN_COMFYUI_URL` | `http://host.docker.internal:8188` | ComfyUI endpoint |
| `BCN_BROWSERLESS_URL` | `http://browserless:3000` | Browserless headless Chromium |
| `BCN_GITHUB_TOKEN` | - | GitHub API token for GHSA |
| `BCN_TWITTER_BEARER_TOKEN` | - | X API bearer token for Twitter/X |
| `BCN_RELEVANCE_THRESHOLD` | `7` | Min score (1-10) for briefing |

---

## A2A Protocol

Each agent exposes a standard A2A interface:
- Agent Card at `GET /.well-known/agent.json`
- Message handling via JSON-RPC

Agents can be discovered and invoked by any A2A-compatible client:
```python
from a2a.client import A2AClient
import httpx

async with httpx.AsyncClient() as http:
    client = await A2AClient.get_client_from_agent_card_url(
        http, "http://localhost:9001"
    )
    response = await client.send_message(request)
```

---

## Project Structure
```
bcn/
  cli.py              CLI commands + daemon mode
  config.py           Pydantic Settings (env vars)
  db.py               asyncpg database layer
  models.py           Pydantic data models
  llm.py              Qwen LLM client (OpenAI-compatible)
  comfyui.py          ComfyUI Flux client
  scraper.py          Browserless headless Chromium scraper
  agents/
    base.py           A2A agent boilerplate
    collector.py      Data collection (GHSA, RSS, Twitter)
    analyst.py        LLM analysis + scoring
    writer.py         Briefing + cover image generation
    distributor.py    Multi-channel distribution
  distributors/
    telegram.py       Telegram Bot API
    email.py          SMTP email
    slack.py          Slack webhook
```
