# Architecture

Broken Cloud News is structured as a control plane that owns workflow state and orchestration, plus deployable services that can run in-process or behind internal HTTP endpoints.

The diagrams below are intended to reflect the current codebase and default Compose topology, not an aspirational future shape.

## Logical Architecture

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
        Ghost["Ghost"]:::output
        Substack["Substack"]:::output
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
    Distributor --> Ghost
    Distributor --> Substack
    Distributor --> Email
```

## Deployment Topology

```mermaid
flowchart TB
    classDef node fill:#eef3f8,stroke:#4c78a8,color:#102a43
    classDef svc fill:#edf7ed,stroke:#2f855a,color:#1c4532
    classDef db fill:#f3e8ff,stroke:#805ad5,color:#44337a
    classDef infra fill:#fff7e6,stroke:#dd6b20,color:#7b341e

    Operator["Operator / CI / cron"]:::node --> ControlPlane

    subgraph ControlPlane["Control plane host"]
        CLI2["bcn CLI"]:::node
        Scheduler["bcn scheduler"]:::node
        Workers["bcn worker --lane …"]:::node
        Daemon["bcn run"]:::node
        Queue["workflow_jobs queue"]:::db
        LocalWorkflow["Workflow orchestration layer"]:::node
        CLI2 --> LocalWorkflow
        Scheduler --> Queue
        Workers --> Queue
        Daemon --> Queue
        Workers --> LocalWorkflow
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
        Bridge["spark_bridge / host-local model bridge"]:::infra
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

## Briefing Execution Flow

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
            D->>CH: send to Telegram / Discord / Ghost / Substack / Email
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

### Control Plane

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

Scheduled jobs are defined through `bcn/workflows/catalog.py`. Each definition declares:

- workflow id
- trigger builder
- logical steps
  - component
  - operation
  - args
- optional enablement predicate
- queue lane / priority / retry policy
- optional deadline budget

The scheduler no longer executes those workflows inline. It enqueues durable jobs into `workflow_jobs`, and lane-scoped workers lease and execute them. Execution is still step-driven through `bcn/workflows/execution.py`, which dispatches typed workflow steps instead of hardcoding one executor per scheduled workflow.

### Deployable Services

Deployable services live under `bcn/services` and are callable either:

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
- `distributor` is a facade over channel-specific adapters in `bcn/distributors/`

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

### Persistence Layer

The persistence layer lives under `bcn/persistence` and is split by domain:

- `news_items.py`
- `briefings.py`
- `training.py`
- `evaluation.py`
- `collection_sources.py`
- `newsletter.py`
- `history.py`
- `runtime.py`
- `optimization.py`

PostgreSQL is the system of record for:

- collected and analyzed items
- briefing lifecycle state
- delivery outcomes
- human reviews
- simulation, benchmark, and shadow runs
- source-review registry state
- optimization runs and candidates

### Evaluation Stack

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
