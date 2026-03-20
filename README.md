<p align="center">
  <img src="assets/logo.png" alt="Broken Cloud News" width="400"/>
</p>

Broken Cloud News is a cloud-security briefing platform with a central control plane, a split persistence layer, and deployable processing services that can run either in-process or behind internal HTTP endpoints.

It collects security signals from GHSA, RSS, Reddit, and Twitter/X, analyzes and ranks them, generates briefings and newsletters, evaluates draft quality, distributes approved output, and stores evaluation and review data for replay and benchmarking.

## System Overview

### Logical Architecture

```mermaid
flowchart LR
    classDef source fill:#f8f9fa,stroke:#6c757d,color:#212529
    classDef control fill:#e7f1ff,stroke:#2f6feb,color:#0b2f6b
    classDef service fill:#eaf7ea,stroke:#2e7d32,color:#1b4332
    classDef store fill:#f7ecff,stroke:#7b2cbf,color:#4a148c
    classDef model fill:#fff4e5,stroke:#ef6c00,color:#7a3b00
    classDef output fill:#fff8dc,stroke:#8d6e00,color:#5d4037

    subgraph Inputs["External Inputs"]
        GHSA["GitHub Security Advisories"]:::source
        RSS["RSS feeds"]:::source
        Reddit["Reddit RSS"]:::source
        Twitter["Twitter/X API"]:::source
    end

    subgraph Control["Control Plane"]
        CLI["CLI"]:::control
        Scheduler["Scheduler / workflow runtime"]:::control
        Workflows["Workflow orchestration"]:::control
        Eval["Evaluation lanes"]:::control
    end

    subgraph Services["Deployable Services"]
        Collector["Collector"]:::service
        Analyst["Analyst"]:::service
        Writer["Writer"]:::service
        Critic["Critic"]:::service
        Verifier["Verifier"]:::service
        Distributor["Distributor"]:::service
    end

    subgraph Storage["Persistence"]
        Postgres[("PostgreSQL")]:::store
        Dashboard["Next.js dashboard"]:::store
    end

    subgraph Models["Model Providers"]
        LLM["LLM endpoints"]:::model
        Image["Image generation"]:::model
    end

    subgraph Outputs["Outbound Channels"]
        Telegram["Telegram"]:::output
        Discord["Discord"]:::output
        Email["Email"]:::output
    end

    GHSA --> Collector
    RSS --> Collector
    Reddit --> Collector
    Twitter --> Collector

    CLI --> Workflows
    Scheduler --> Workflows
    Eval --> Workflows

    Workflows --> Collector
    Workflows --> Analyst
    Workflows --> Writer
    Workflows --> Distributor
    Writer --> Critic
    Writer --> Verifier

    Collector --> Postgres
    Analyst --> Postgres
    Distributor --> Postgres
    Eval --> Postgres
    Dashboard --> Postgres

    LLM --> Analyst
    LLM --> Writer
    LLM --> Critic
    LLM --> Verifier
    Image --> Writer

    Distributor --> Telegram
    Distributor --> Discord
    Distributor --> Email
```

### Deployment Topology

```mermaid
flowchart TB
    classDef node fill:#eef3f8,stroke:#4c78a8,color:#102a43
    classDef svc fill:#edf7ed,stroke:#2f855a,color:#1c4532
    classDef db fill:#f3e8ff,stroke:#805ad5,color:#44337a
    classDef infra fill:#fff7e6,stroke:#dd6b20,color:#7b341e

    Operator["Operator / CI / cron"]:::node --> ControlPlane

    subgraph ControlPlane["Control plane host"]
        CLI2["bcn CLI"]:::node
        Daemon["bcn run"]:::node
        LocalWorkflow["Workflow layer"]:::node
        CLI2 --> LocalWorkflow
        Daemon --> LocalWorkflow
    end

    subgraph ServicePool["Optional remote service pools"]
        CollectorPool["collector instances"]:::svc
        AnalystPool["analyst instances"]:::svc
        WriterPool["writer instances"]:::svc
        CriticPool["critic instances"]:::svc
        VerifierPool["verifier instances"]:::svc
        DistributorPool["distributor instances"]:::svc
    end

    subgraph SharedInfra["Shared infrastructure"]
        PG[("PostgreSQL")]:::db
        Dash["dashboard"]:::node
        Proxy["egress_proxy"]:::infra
        DNS["dns_resolver"]:::infra
        Bridge["spark_bridge"]:::infra
    end

    LocalWorkflow --> CollectorPool
    LocalWorkflow --> AnalystPool
    LocalWorkflow --> WriterPool
    LocalWorkflow --> DistributorPool
    WriterPool --> CriticPool
    WriterPool --> VerifierPool

    LocalWorkflow --> PG
    Dash --> PG
    LocalWorkflow --> Proxy
    LocalWorkflow --> DNS
    LocalWorkflow --> Bridge
```

### Briefing Execution Flow

```mermaid
sequenceDiagram
    participant S as Scheduler / CLI
    participant W as Workflow orchestration
    participant C as Collector
    participant A as Analyst
    participant DB as PostgreSQL
    participant WR as Writer
    participant CR as Critic
    participant VE as Verifier
    participant D as Distributor
    participant CH as Channels

    S->>W: start pipeline or scheduled run
    W->>C: collect(source)
    C-->>W: normalized items
    W->>DB: insert news_items

    W->>DB: claim unanalyzed items
    W->>A: analyze_item(item)
    A-->>W: summary, score, canonical URL
    W->>DB: persist analyzed update

    W->>DB: load eligible items + recent published history
    W->>WR: select_items_for_workflow(..., recent_published)
    WR-->>W: selection plan

    alt selection says generate
        W->>DB: load recent briefing history
        W->>WR: generate_release_candidate(selected_items, history, mode)
        WR->>WR: draft + postprocess

        loop rewrite loop until pass or max rounds
            WR->>CR: evaluate(draft)
            CR-->>WR: critique result
            WR->>VE: evaluate(draft)
            VE-->>WR: verification result
            WR->>WR: apply rewrite feedback
        end

        WR-->>W: candidate markdown + selected items + gate state

        alt candidate is publishable
            W->>WR: build_release_artifact(...)
            WR-->>W: markdown/html/cover payload
            W->>DB: persist briefing + generation trace
            W->>DB: claim draft for distribution
            W->>D: deliver(briefing)
            D->>CH: send to Telegram / Discord / Email
            D-->>W: delivery results
            W->>DB: persist outcomes + mark distributed
        else blocked after review
            W->>DB: persist blocked generation trace
            W-->>S: blocked reason
        end
    else selection says skip
        W-->>S: skip reason
    end
```

## Current Runtime Model

### Control plane

The control plane owns:

- scheduling
- scheduled workflow definitions
- workflow orchestration
- retry and stale-claim recovery
- database state transitions
- generation trace persistence
- human review and evaluation orchestration
- retrieval of novelty/history context for downstream services

The control plane lives under `bcn/workflows` and is the only layer that mutates workflow state.

Scheduled jobs are defined through a small workflow catalog in `bcn/workflows/catalog.py`. Each definition declares:

- workflow id
- trigger builder
- logical steps
  - component
  - operation
  - args
- optional enablement predicate

Execution is step-driven through `bcn/workflows/execution.py`, which dispatches typed workflow steps instead of hardcoding one executor per scheduled workflow.

### Deployable services

The deployable services live under `bcn/services` and are designed to be callable either:

- directly in-process through the service registry
- remotely over internal JSON/HTTP endpoints

Current service set:

| Service | Default port | Canonical endpoint(s) | Responsibility |
|---|---:|---|---|
| `writer` | `8081` | `/v1/trace-metadata`, `/v1/select-items-for-workflow`, `/v1/evaluate-existing-markdown`, `/v1/generate-release-candidate`, `/v1/build-release-artifact`, `/v1/simulate-briefing-body` | selection planning, novelty-aware draft generation, artifact rendering, simulation |
| `critic` | `8082` | `/v1/evaluate` | editorial and quality evaluation |
| `verifier` | `8083` | `/v1/evaluate` | deterministic and LLM-backed factual verification |
| `collector` | `8084` | `/v1/collect` | source collection, normalization, and bounded enrichment |
| `analyst` | `8085` | `/v1/analyze-item` | scoring, summarization, tagging, canonicalization |
| `distributor` | `8086` | `/v1/deliver` | outbound delivery to Telegram, Discord, Ghost, Substack, and email |

Every service also exposes `/v1/healthz`.

Internal service structure:

- `writer` is a facade over `selection`, `drafting`, `review`, `postprocess`, `rendering`, and `covers`
- `collector` is a facade over source adapters in `bcn/services/collector/{ghsa,rss,reddit,twitter}.py`
- `critic` and `verifier` are review services typically called by the writer, not directly by the workflow layer

Service boundary rule:

- the control plane loads workflow state and history from PostgreSQL
- services operate on explicit request payloads
- novelty/repetition policy lives in writer and critic logic, not in the control plane

Transport characteristics:

- ASGI HTTP server
- one app-scoped component instance per process, created at startup and closed at shutdown
- JSON request/response payloads
- optional shared auth token via `X-BCN-Service-Token` or `Authorization: Bearer ...`
- service contracts defined in `bcn/contracts`

### Persistence layer

The persistence layer lives under `bcn/persistence` and is split by domain:

- `news_items.py`
- `briefings.py`
- `training.py`
- `evaluation.py`
- `collection_sources.py`
- `newsletter.py`
- `history.py`
- `runtime.py`

PostgreSQL is the system of record for:

- collected and analyzed items
- briefing lifecycle state
- delivery outcomes
- human reviews
- simulation, benchmark, and shadow runs
- source-review registry state

### Evaluation stack

The evaluation layer lives under `bcn/evaluation` and reuses the same service contracts as live execution.

It supports:

- historical simulation
- benchmark packs
- challenger evaluations
- shadow runs
- comparative scoring across stored runs

The dashboard in `dashboard/` reads persisted evaluation data from PostgreSQL.

## Repository Layout

| Path | Purpose |
|---|---|
| `bcn/workflows/` | control-plane orchestration, scheduled job catalog, and workflow runtimes |
| `bcn/services/` | deployable processing services |
| `bcn/contracts/` | typed cross-service payloads and protocols |
| `bcn/persistence/` | database access layer |
| `bcn/transports/http/` | ASGI servers and remote clients |
| `bcn/evaluation/` | simulation, benchmark, and shadow lanes |
| `dashboard/` | Next.js evaluation dashboard |
| `postgres/migrations/` | schema migrations |
| `infra/` | proxy, DNS, and bridge config used by Compose |

## Running The System

### Prerequisites

- Python 3.12+
- Docker and Docker Compose
- PostgreSQL
- configured LLM endpoints
- configured channel credentials if distribution is enabled

### Local setup

```bash
git clone https://github.com/andreyka/broken-cloud-news.git
cd broken-cloud-news
./setup.sh
```

### Start the default stack

```bash
docker compose up -d
```

Current Compose services:

- `bcn`
- `postgres`
- `dashboard`
- `egress_proxy`
- `dns_resolver`
- `spark_bridge`

Default exposed ports:

- `3007` -> dashboard
- `5432` -> PostgreSQL
- `9001`-`9006` -> BCN container published ports

### CLI entrypoints

Core workflow commands:

```bash
bcn collect --source all
bcn analyze
bcn write --mode regular_daily_briefing
bcn distribute --mode regular_daily_briefing
bcn workflow-run --mode ad_hoc
bcn pipeline --mode regular_daily_briefing
bcn run
```

Evaluation and review commands:

```bash
bcn simulate --limit 30
bcn benchmark-pack --output benchmark_pack.json
bcn benchmark --cases benchmark_pack.json
bcn shadow --candidate-overrides challenger.json --store-db
bcn review --decision accept
bcn review-queue --only-unreviewed
bcn export-training --output-dir training_export
```

Service hosting:

```bash
bcn serve writer
bcn serve critic
bcn serve verifier
bcn serve collector
bcn serve analyst
bcn serve distributor
```

## Split Deployment

The control plane can keep workflow ownership while calling remote service pools.

Example layout:

- host A: `bcn run` plus PostgreSQL access
- host B: `bcn serve writer`
- host C: `bcn serve critic`
- host D: `bcn serve verifier`
- host E: `bcn serve collector`
- host F: `bcn serve analyst`
- host G: `bcn serve distributor`

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

## Source And Output Coverage

### Inputs

- GitHub Security Advisories
- RSS feeds
- Reddit RSS
- Twitter/X API

### Outputs

- Telegram
- Discord
- Email newsletters

## Security And Networking

The default Compose topology includes:

- an internal Squid proxy for outbound web traffic
- an internal CoreDNS resolver
- an internal bridge for the Spark/Qwen endpoint

This keeps the main `bcn` container on an internal network while still allowing controlled outbound access and model access paths.

## Database Lifecycle

Run migrations with:

```bash
bcn db-migrate
```

Preview pending migrations with:

```bash
bcn db-migrate --dry-run
```
