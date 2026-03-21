<p align="center">
  <img src="assets/logo.png" alt="Broken Cloud News" width="400"/>
</p>

Broken Cloud News is a cloud-security briefing platform with a central control plane, a split persistence layer, and deployable processing services that can run either in-process or behind internal HTTP endpoints.

It collects security signals from GHSA, RSS, Reddit, and Twitter/X, analyzes and ranks them, generates briefings and newsletters, evaluates draft quality, distributes approved output, and stores evaluation and review data for replay and benchmarking.

## What It Does

- collects and normalizes security signals from GHSA, RSS, Reddit, and Twitter/X
- analyzes, scores, and canonicalizes items before generation
- generates daily briefings and monthly newsletters with critic/verifier loops
- distributes approved output to Telegram, Discord, Ghost, Substack, and email
- stores generation, review, distribution, and evaluation data for replay, shadowing, and benchmarking

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md): system model, deployable services, persistence boundaries, and diagrams
- [docs/operations.md](docs/operations.md): setup, Compose topology, environment variables, networking, and deployment notes
- [docs/cli.md](docs/cli.md): CLI command reference, evaluation lanes, and operator/admin commands
- [bcn/evaluation/README.md](bcn/evaluation/README.md): evaluation concepts and implementation notes
- [benchmark_packs/README.md](benchmark_packs/README.md): benchmark-pack format and usage
- [docs/optimization-loop.md](docs/optimization-loop.md): offline optimization loop

## Quick Start

Supported local path:

```bash
git clone https://github.com/andreyka/broken-cloud-news.git
cd broken-cloud-news
./setup.sh
```

If you already have a populated `.env`, start the default stack with:

```bash
docker compose up -d
```

Then verify the stack:

```bash
docker compose ps
```

Default local endpoints:

- `http://localhost:3007` -> dashboard
- `localhost:5432` -> PostgreSQL
- `localhost:9001`-`9006` -> BCN component ports published from the `bcn` container

## Common Commands

```bash
bcn collect --source all
bcn analyze
bcn write --mode regular_daily_briefing
bcn shadow --candidate-overrides bcn/config/shadow_nemotron_spark.json --store-db
bcn simulate --limit 30
bcn benchmark --cases benchmark_packs/core_v1.json
```

The full CLI surface, including recovery and review commands, is documented in [docs/cli.md](docs/cli.md).

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
| `infra/` | proxy, DNS, and bridge config used by Compose |

## Notes

- `setup.sh` is the supported bootstrap path for local development because it writes the `.env` file consumed by Compose.
- The architecture diagrams were moved to [ARCHITECTURE.md](ARCHITECTURE.md) so they can stay detailed without turning this file into an operator runbook.
