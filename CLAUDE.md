# CLAUDE.md — Claude Code Project Instructions

This file provides context for Claude Code when working with the Broken Cloud News (BCN) codebase.

## Project Overview

BCN is an agentic cloud security briefing system that autonomously collects, analyzes, and distributes security news. It produces daily briefings (Telegram/Discord) and monthly newsletters (email) using six A2A protocol agents: Collector, Analyst, Writer, Critic, Verifier, and Distributor.

## Tech Stack

- **Language:** Python 3.12+
- **Framework:** A2A SDK (Google Agent-to-Agent protocol), Starlette/Uvicorn
- **Database:** PostgreSQL (asyncpg)
- **LLM Providers:** OpenAI-compatible, Google Vertex AI / Gemini, Anthropic Claude
- **Image Generation:** ComfyUI (Flux.1-schnell), Gemini image APIs
- **Web Scraping:** Playwright (headless Chromium)
- **Config:** Pydantic Settings (`BCN_` prefixed env vars)
- **Testing:** pytest, pytest-asyncio, respx

## Repository Layout

```
bcn/
  cli.py              # CLI entry point (click)
  common/
    config.py         # Pydantic Settings (all BCN_* env vars)
    db.py             # asyncpg database layer
    models.py         # Pydantic data models
    llm.py            # Role-aware LLM router (openai_compat/gemini/vertexai/anthropic)
    comfyui.py        # ComfyUI Flux client
    scraper.py        # Playwright content fetcher
    url_policy.py     # SSRF policy + URL normalization
  agents/
    base.py           # A2A agent boilerplate
    collector/        # Data collection (GHSA, RSS, Reddit, Twitter/X)
    analyst/          # LLM relevance scoring + summarization
    writer/           # Briefing + cover image generation
    critic/           # Quality assessment
    verifier/         # Fact-checking + link hygiene
    distributor/      # Multi-channel distribution
  briefing/
    selection.py      # Ranking + diversity-aware item selection
    quality.py        # Deterministic quality gate
    verifier.py       # Fact-check orchestration
    text.py           # Markdown normalization
  workflows/
    automation.py     # Scheduler jobs
    modes/            # Workflow modes (daily, monthly, ad-hoc)
  distributors/       # Channel implementations (telegram, discord, email, slack)
tests/                # pytest test suite
```

## Build & Test Commands

```bash
# Install (editable with dev deps)
pip install -e ".[dev]"

# Run all tests
python -m pytest tests/ -q

# Run specific test file
python -m pytest tests/test_llm.py -q

# Run daemon (all agents + scheduler)
bcn run

# CLI commands
bcn collect              # Collect from all sources
bcn analyze              # Analyze new items
bcn write --mode regular_daily_briefing
bcn pipeline --mode regular_daily_briefing
```

## Architecture Patterns

- **LLM Provider Routing:** `bcn/common/llm.py` supports four providers: `openai_compat`, `gemini`, `vertexai`, and `anthropic`. Each agent role (analyst, writer, critic, verifier, cover) can use a different provider/model via `BCN_LLM_PROVIDER_{ROLE}`, `BCN_LLM_MODEL_{ROLE}`, etc.
- **Agent Communication:** All agents use A2A JSON-RPC over HTTP. Each agent has a card (`/.well-known/agent.json`) and handles `POST /message`.
- **State Machine:** `news_items.status` transitions: NEW → ANALYZING → ANALYZED → WRITING → PUBLISHED (or terminal failure states).
- **Config:** All configuration via `BCN_` prefixed environment variables. See `bcn/common/config.py` for the full schema.

## Key Design Decisions

- Agents are independent HTTP servers sharing state through PostgreSQL (not in-memory).
- The LLM client uses httpx for OpenAI-compatible and Anthropic endpoints, and the google-genai SDK for Gemini/Vertex.
- Quality gates combine deterministic checks with LLM-based assessment.
- The writer→critic loop runs up to `BCN_BRIEFING_CRITIQUE_MAX_ROUNDS` iterations.
- Cover images fall back from Gemini image API → ComfyUI automatically.

## Common Pitfalls

- The `conftest.py` fixture includes fields (`apify_token`, `browserless_url`) that no longer exist in `Settings`; they are silently ignored by Pydantic.
- The `test_config.py::TestSettings::test_defaults` test expects `llm_timeout == 120` but the actual default is `180`. This is a pre-existing issue.
- Playwright must be installed separately: `playwright install chromium`.
- The database schema is managed by runtime migrations in `bcn/common/db.py`.

## Using Claude as an LLM Provider

To use Claude models for BCN agent roles, set the provider to `anthropic`:

```bash
BCN_LLM_PROVIDER=anthropic
BCN_LLM_BASE_URL=https://api.anthropic.com
BCN_LLM_API_KEY=sk-ant-...
BCN_LLM_MODEL=claude-sonnet-4-20250514
```

Claude does not support image generation, so the cover role should use a different provider (Gemini/Vertex or ComfyUI fallback).
