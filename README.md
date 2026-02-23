<p align="center">
  <img src="assets/logo.png" alt="Broken Cloud News" width="400"/>
</p>

---

## Architecture

Four A2A agents work together, coordinated by an internal scheduler. Each agent runs as an independent HTTP server using the Google Agent-to-Agent protocol.

```mermaid
flowchart TB
    subgraph Sources["Data Sources"]
        GHSA["GitHub Security\nAdvisories"]
        RSS["RSS Feeds\n(CISA, AWS, Cloudflare)"]
        Reddit["Reddit RSS\n(r/netsec + cloud subreddits)"]
        Twitter["Twitter/X\n(X API v2)"]
    end

    subgraph BCN["Broken Cloud News Agents"]
        direction TB
        Collector["Collector\n:9001"]
        DB[("PostgreSQL\nnews_items\nbriefings")]
        Analyst["Analyst\n:9002"]
        Writer["Writer\n:9003"]
        Critic["Critic\n:9005"]
        Distributor["Distributor\n:9004"]
    end

    subgraph AI["DGX Spark"]
        Qwen["Qwen3-VL\n(vLLM :8000)"]
        Flux["Flux.1-schnell\n(ComfyUI :8188)"]
    end

    subgraph Channels["Distribution"]
        TG["Telegram\nChannel"]
        Email["Email\n(SMTP)"]
        Slack["Slack\n(Webhook)"]
    end

    GHSA --> Collector
    RSS --> Collector
    Reddit --> Collector
    Twitter --> Collector
    Collector -- "store items" --> DB
    DB -- "unanalyzed items" --> Analyst
    Analyst -- "score & summarize" --> Qwen
    Qwen -- "analysis result" --> Analyst
    Analyst -- "update scores/tags" --> DB
    DB -- "top scored items" --> Writer
    Writer -- "generate briefing" --> Qwen
    Writer -- "draft" --> Critic
    Critic -- "quality feedback" --> Writer
    Writer -- "generate cover" --> Flux
    Qwen -- "briefing text" --> Writer
    Flux -- "cover image" --> Writer
    Writer -- "store briefing" --> DB
    DB -- "latest briefing" --> Distributor
    Distributor --> TG
    Distributor -.-> Email
    Distributor -.-> Slack
```

### Agent Details

| Agent | Port | Trigger | Role |
|-------|------|---------|------|
| **Collector** | 9001 | Every 2-6h | Fetches GHSA, RSS (CISA/AWS/Cloudflare), Reddit RSS, Twitter/X via API v2 |
| **Analyst** | 9002 | Every 15m | Scores relevance (1-10) and summarizes via Qwen LLM |
| **Writer** | 9003 | Daily 9:00 | Generates briefing + Flux cover image from top items |
| **Critic** | 9005 | On-demand | Scores/criticizes briefing quality (LLM + deterministic gate) |
| **Distributor** | 9004 | After Writer | Sends photo+caption to Telegram channel |

All agents communicate via the **A2A JSON-RPC protocol** and share state through PostgreSQL. The scheduler orchestrates the pipeline automatically in daemon mode.

---

## Quick Start

### Prerequisites
- Python 3.12+
- Docker & Docker Compose
- NVIDIA DGX / GPU server with Qwen + ComfyUI deployed (see [AI Infrastructure](#ai-infrastructure))

### Setup
```bash
git clone https://github.com/andreyka/broken-cloud-news.git
cd broken-cloud-news

# Start PostgreSQL
docker compose up -d postgres

# Install Python package
pip install -e .
playwright install chromium

# Configure
cp .env.example .env
# Edit .env with your tokens and endpoints
```

### Run

**CLI commands:**
```bash
bcn collect              # Collect from all sources
bcn collect -s ghsa      # Collect GHSA only
bcn collect -s reddit    # Collect Reddit RSS only
bcn analyze              # Analyze new items with LLM
bcn write                # Generate briefing + cover image
bcn critique --latest    # Critique latest briefing (quality report JSON)
bcn critique --file ./draft.md
bcn simulate --limit 30 --output simulation_report.json  # Backtest vs historical briefings (no publish)
bcn simulate --limit 0 --with-critic-rewrites            # Full heavy replay with writer->critic rewrites
bcn simulate --store-db                                   # Persist run/results in DB and compare with previous run
bcn review --decision accept --issue-tag style            # Store human review labels for latest briefing
bcn review-queue --only-unreviewed                        # List briefings needing manual review
bcn record-outcome --briefing-id <uuid> --channel telegram --views 1200 --clicks 74
bcn export-training --output-dir training_export          # Export SFT + preference JSONL datasets
bcn distribute           # Send to configured channels
bcn pipeline             # Full pipeline: collect -> analyze -> write -> distribute
```

**Daemon mode** (all agents + scheduler):
```bash
bcn run
```

**Docker (full stack):**
```bash
docker compose up -d
```

---

## Configuration

All settings via environment variables with `BCN_` prefix. See `.env.example` for the full list.

| Variable | Default | Description |
|----------|---------|-------------|
| `BCN_DATABASE_URL` | `postgresql://...` | PostgreSQL connection string |
| `BCN_LLM_BASE_URL` | `http://192.168.0.9:8000/v1` | Qwen API endpoint |
| `BCN_COMFYUI_URL` | `http://192.168.0.9:8188` | ComfyUI Flux endpoint |
| `BCN_GITHUB_TOKEN` | - | GitHub API token for GHSA |
| `BCN_TWITTER_BEARER_TOKEN` | - | X API v2 bearer token |
| `BCN_TELEGRAM_BOT_TOKEN` | - | Telegram bot token |
| `BCN_TELEGRAM_CHAT_ID` | - | Telegram channel chat ID |
| `BCN_RELEVANCE_THRESHOLD` | `7` | Min score (1-10) to include in briefing |

Important briefing-quality knobs:
- `BCN_BRIEFING_MAX_RSS_ITEMS` (default `3`) limits RSS dominance.
- `BCN_BRIEFING_MAX_ITEMS_PER_DOMAIN` (default `2`) prevents single-domain monoculture.
- `BCN_BRIEFING_MIN_SELECTED_ITEMS` (default `1`) allows one-item briefings on low-volume days.
- `BCN_BRIEFING_MIN_CHARS`/`BCN_BRIEFING_TARGET_CHARS`/`BCN_BRIEFING_HARD_MAX_CHARS`
  (defaults `1200`/`1700`/`2300`) increase depth while keeping Telegram-safe output.
- `BCN_BRIEFING_SINGLE_ITEM_MIN_CHARS`/`BCN_BRIEFING_SINGLE_ITEM_TARGET_CHARS`/
  `BCN_BRIEFING_SINGLE_ITEM_HARD_MAX_CHARS` relax depth limits for single-item days.
- `BCN_BRIEFING_SKIP_IF_NO_HIGH_SIGNAL` + `BCN_BRIEFING_MIN_HIGH_SIGNAL_TO_PUBLISH`
  can skip a day entirely when there is no truly actionable signal.
- `BCN_BRIEFING_SOCIAL_PROOF_WEIGHT` + `BCN_BRIEFING_SOCIAL_PROOF_MAX_BONUS`
  add bounded engagement influence (likes/retweets/upvotes/comments) to ranking.
- `BCN_BRIEFING_CRITIQUE_MAX_ROUNDS` (default `5`) sets max writer rewrites in the
  writer->critic loop before publishing the best available draft.
- `BCN_BRIEFING_GATE_MODE` (default `balanced`) controls deterministic strictness:
  `strict` (structure rules are blocking), `balanced` (structure/style are advisory),
  `minimal` (only hard correctness checks block).
- `BCN_SCRAPE_PLAYWRIGHT_FETCH_FALLBACK` (default `true`) uses Playwright request
  fallback when direct HTTP fetches of feeds/Reddit endpoints fail.

---

## AI Infrastructure (DGX Spark)

### 1. Qwen3-VL (LLM Inference)

Runs as an OpenAI-compatible API via **vLLM**:

```bash
docker run --rm -it \
    --gpus all \
    --ipc=host \
    --shm-size=32g \
    -p 8000:8000 \
    -v ~/.cache/huggingface:/root/.cache/huggingface \
    nvcr.io/nvidia/vllm:25.12.post1-py3 \
    vllm serve Qwen/Qwen3-VL-30B-A3B-Instruct-FP8 \
    --host 0.0.0.0 --port 8000 \
    --trust-remote-code \
    --dtype auto \
    --gpu-memory-utilization 0.55 \
    --max-model-len 16384
```

### 2. Flux.1-schnell (Cover Image Generation)

Runs **ComfyUI** with Flux model:

```bash
# Build image
docker build -t comfyui:arm64-cuda -f Dockerfile.comfyui .

# Download model
mkdir -p ~/comfyui/models/checkpoints
wget -O ~/comfyui/models/checkpoints/flux1-schnell.safetensors \
  https://huggingface.co/black-forest-labs/FLUX.1-schnell/resolve/main/flux1-schnell.safetensors

# Run
docker run --rm -it \
  --gpus all --shm-size=32g \
  -p 8188:8188 \
  -v ~/comfyui/models:/opt/ComfyUI/models \
  -v ~/comfyui/output:/opt/ComfyUI/output \
  comfyui:arm64-cuda
```

---

## A2A Protocol

Each agent exposes a standard Google A2A interface:
- Agent Card at `GET /.well-known/agent.json`
- Message handling via JSON-RPC

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
  cli.py              CLI commands + daemon scheduler
  config.py           Pydantic Settings (env vars)
  db.py               asyncpg database layer
  models.py           Pydantic data models
  llm.py              Qwen LLM client (analysis + briefing)
  simulation.py       Historical briefing replay + comparison scoring
  comfyui.py          ComfyUI Flux client (cover images)
  scraper.py          Playwright headless Chromium scraper
  agents/
    base.py           A2A agent boilerplate
    collector.py      Data collection (GHSA, RSS, Reddit, Twitter/X)
    analyst.py        LLM relevance scoring + summarization
    writer.py         Briefing + cover image generation
    critic.py         Briefing critique and quality assessment
    distributor.py    Multi-channel distribution
  briefing/
    selection.py      Ranking + diversity-aware item selection
    quality.py        Deterministic quality gate checks
    text.py           Markdown normalization and fallback formatting
  distributors/
    telegram.py       Telegram Bot API (photo + caption)
    email.py          SMTP email
    slack.py          Slack webhook
assets/
  logo.png            Project logo
```
