# Broken Cloud News — Security/Architecture Review Briefing

## Executive Summary

The project has a strong functional foundation (modular agents, SSRF policy utilities, good unit-test coverage), but there are several implementation gaps that can block production-grade reliability and security.  
The most material risks are **network exposure without authentication**, **weak secret defaults**, and **operational fragility in the single-process daemon mode**.

## Key Obstacles to the Project Goal

### 1) A2A agent endpoints are network-exposed without auth (**High**)
- **Where:** `bcn/agents/base.py:84-88`, `docker-compose.yaml:56-62`
- **What:** Servers bind to `0.0.0.0` and ports 9001–9006 are published.
- **Why this blocks the goal:** External callers can invoke agent skills unless deployment network controls are perfect; this undermines trust in briefing integrity and system safety.
- **Suggested fix:** Add service-level auth (token/mTLS) and keep ports internal by default.

### 2) Default database credentials are hardcoded (**High**)
- **Where:** `docker-compose.yaml:6-8`, `bcn/common/config.py:188`
- **What:** Username/password defaults are committed and reused across config paths.
- **Why this blocks the goal:** Easy credential reuse and accidental production carry-over increase breach blast radius.
- **Suggested fix:** Remove insecure defaults, require env-injected secrets (or Docker/K8s secret stores).

### 3) Single-process daemon reduces fault isolation (**High**)
- **Where:** `bcn/cli.py:1568-1589`
- **What:** All six agents are started as tasks in one process under `bcn run`.
- **Why this blocks the goal:** One process-level failure can disrupt the full pipeline (collector → analyst → writer → distributor).
- **Suggested fix:** Prefer one agent per process/container in production; add supervisor-level restart policies.

### 4) No health/readiness endpoints for agents (**Medium**)
- **Where:** `bcn/agents/base.py` (app wiring only)
- **What:** No explicit `/health`/`/ready` contract for orchestration checks.
- **Why this blocks the goal:** Hard to operate safely at scale; startup/partial-failure detection becomes guesswork.
- **Suggested fix:** Add lightweight health/readiness routes and wire them into deployment probes.

### 5) Broad exception handling around critical writer flows (**Medium**)
- **Where:** `bcn/agents/writer/agent.py` (e.g. `110-122`, `175-181`, `516-527`, `963-1050`)
- **What:** Multiple `except Exception` blocks around generation/finalization/release paths.
- **Why this blocks the goal:** Pipeline can continue in degraded states; root cause analysis gets harder under incident pressure.
- **Suggested fix:** Narrow exception scopes for expected failures and emit structured failure states/metrics.

### 6) Email distribution leaks recipient list to all recipients (**Medium**)
- **Where:** `bcn/distributors/email.py:72`
- **What:** Recipients are joined into the `To` header.
- **Why this blocks the goal:** Privacy/compliance risk for newsletter subscribers.
- **Suggested fix:** Use per-recipient sends or BCC strategy.

### 7) Container build installs pre-release dependencies (**Medium**)
- **Where:** `Dockerfile:8`
- **What:** `pip install --pre .`
- **Why this blocks the goal:** Raises production instability risk via alpha/beta dependency resolution.
- **Suggested fix:** Remove `--pre` and lock production dependency versions.

### 8) Test strategy is strong for units but thin for full workflow behavior (**Medium**)
- **Where:** `tests/` (unit-focused coverage; no end-to-end pipeline test)
- **What:** Current tests validate components well, but not complete pipeline orchestration behavior.
- **Why this blocks the goal:** Integration failures across agent boundaries are likely to appear only in runtime.
- **Suggested fix:** Add one deterministic end-to-end happy-path test with mocked external services.

## Practical Priority Order

1. **Lock down exposure first:** endpoint auth + internal networking defaults.
2. **Fix secret handling:** remove hardcoded DB defaults.
3. **Improve survivability:** split daemon responsibilities and add health probes.
4. **Harden delivery quality:** narrow broad exception handling in writer.
5. **Address privacy/ops debt:** email recipient privacy + remove pre-release deps.
6. **Close confidence gap:** add at least one full pipeline integration test.
