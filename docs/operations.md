# Operations

## Prerequisites

- Python 3.12+
- Docker and Docker Compose
- PostgreSQL
- configured LLM endpoints
- configured channel credentials for any enabled distributors

## Supported Local Setup Path

The supported bootstrap path is:

```bash
git clone https://github.com/andreyka/broken-cloud-news.git
cd broken-cloud-news
./setup.sh
```

`setup.sh` writes the `.env` file consumed by Compose, validates dependencies, and can start the stack for you.

Useful setup modes:

```bash
./setup.sh --check
./setup.sh --reset
./setup.sh --nuke
```

## Manual Setup

If you do not use `setup.sh`, create `.env` yourself before starting Compose.

The easiest starting point is:

```bash
cp .env.example .env
```

Then populate the values required for your deployment. The Compose stack uses `env_file: .env` for both `bcn` and `dashboard`.
The worker split also loads the same `.env` for `scheduler`, `ingest_worker`, and
`evaluation_worker`.

## Start The Default Stack

```bash
docker compose up -d
```

Current Compose services:

- `scheduler`
- `bcn`
- `ingest_worker`
- `evaluation_worker`
- `postgres`
- `dashboard`
- `egress_proxy`
- `dns_resolver`
- `spark_bridge`

Default exposed ports:

- `3007` -> dashboard
- `5432` -> PostgreSQL

## Default Compose Topology

The default stack in `docker-compose.yaml` runs:

- `postgres` for the system of record
- `scheduler` with the `scheduler` command as the enqueue-only control plane
- `bcn` with `worker --lane publish`
- `ingest_worker` with `worker --lane collection --lane analysis`
- `evaluation_worker` with `worker --lane evaluation`
- `dashboard` against the same database
- `egress_proxy` as the outbound web proxy
- `dns_resolver` as the internal DNS resolver
- `spark_bridge` as an internal bridge to a host-local OpenAI-compatible model endpoint

Every BCN runtime container is configured with:

- `HTTP_PROXY` / `HTTPS_PROXY` pointing at `egress_proxy`
- `BCN_PLAYWRIGHT_PROXY` pointing at the same proxy by default
- internal DNS through `dns_resolver`

That proxy path matters for Playwright-backed operations such as:

- fallback web scraping
- Substack publishing

Queue control surface:

- `bcn workflow-lanes list`
- `bcn workflow-lanes pause <lane> --reason "..."`
- `bcn workflow-lanes resume <lane>`

The dashboard now shows queue alerts, lane pause state, and workflow job drill-down
pages for failed jobs at `/jobs/<job-id>`.

## Model Endpoints

BCN supports:

- a single default LLM endpoint via `BCN_LLM_*`
- optional per-role overrides such as `BCN_LLM_MODEL_WRITER`, `BCN_LLM_MODEL_CRITIC`, and `BCN_LLM_MODEL_VERIFIER`
- OpenAI-compatible upstreams through `BCN_LLM_PROVIDER=openai_compat`
- hosted providers such as Vertex AI through per-role overrides

If you proxy a host-local model into the Compose network, use:

```bash
SPARK_BRIDGE_UPSTREAM=http://host.docker.internal:8000
```

The bridge is model-agnostic. It can point at Qwen, Nemotron, or any other compatible upstream.

## Distribution Channels

Current wired daily-briefing channels are:

- Telegram
- Discord
- Ghost
- Substack

Monthly newsletters use:

- email

Relevant `.env` sections live in [.env.example](/mnt/d/dev/github/broken-cloud-news/.env.example).

Operational note:

- Ghost publishing requires `BCN_GHOST_ENABLED=true`, `BCN_GHOST_ADMIN_API_URL`, and `BCN_GHOST_ADMIN_API_KEY`
- Substack publishing requires `BCN_SUBSTACK_ENABLED=true`, `BCN_SUBSTACK_SID`, and `BCN_SUBSTACK_PUBLICATION_URL`
- Substack cover-image hosting currently depends on Ghost being configured, because BCN uses Ghost-hosted public image URLs for Substack cover embedding

## Split Deployment

The control plane can keep workflow ownership while calling remote service pools.

Example layout:

- host A: `bcn scheduler` plus PostgreSQL access
- host B: `bcn worker --lane publish`
- host C: `bcn worker --lane collection --lane analysis`
- host D: `bcn worker --lane evaluation`
- host E: `bcn serve writer`
- host F: `bcn serve critic`
- host G: `bcn serve verifier`
- host H: `bcn serve collector`
- host I: `bcn serve analyst`
- host J: `bcn serve distributor`

Typical control-plane environment:

```bash
BCN_WRITER_SERVICE_URL=http://writer.internal:8081
BCN_CRITIC_SERVICE_URL=http://critic.internal:8082
BCN_VERIFIER_SERVICE_URL=http://verifier.internal:8083
BCN_COLLECTOR_SERVICE_URL=http://collector.internal:8084
BCN_ANALYST_SERVICE_URL=http://analyst.internal:8085
BCN_DISTRIBUTOR_SERVICE_URL=http://distributor.internal:8086
BCN_SERVICE_AUTH_TOKEN=shared-internal-token
```

Service processes load component-scoped settings rather than the full control-plane settings surface.

Operational note:

- `bcn run` is still valid for a single-host compatibility mode, but the default
  Compose stack now uses dedicated scheduler and worker processes because the
  durable queue is designed to isolate publish, collection, analysis, and
  evaluation work from one another.

## Security And Networking

The default Compose topology includes:

- an internal Squid proxy for outbound web traffic
- an internal CoreDNS resolver
- an internal bridge for a host-local model endpoint

This keeps the main `bcn` container on an internal network while still allowing controlled outbound access and model access paths.

## Database Lifecycle

Apply migrations:

```bash
bcn db-migrate
```

Preview pending migrations:

```bash
bcn db-migrate --dry-run
```

## Related Docs

- [../ARCHITECTURE.md](../ARCHITECTURE.md)
- [cli.md](cli.md)
- [../bcn/evaluation/README.md](../bcn/evaluation/README.md)
