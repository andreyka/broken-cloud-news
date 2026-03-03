# NEXT STEPS: BCN Briefing Quality + Scale Plan

## Main Goal
Create an agentic briefing system that consistently produces cloud-security briefings on par with strong security engineers and tech writers, while remaining reliable and scalable in production.

## Highest Priority (Do First)
Phase 1.5 (`LLM Rate-Limit Stability`) is the current top priority and should be executed before any other phase.

## Phase 0: Stop the Biggest Quality/Workflow Regressions (Immediate)

1. Fix item suppression bug in writer selection.
- Problem: `news_items` are excluded if they appear in any briefing, including failed `DRAFT` ones.
- Action: only exclude items already used in successfully distributed briefings.
- Exit criteria: failed distribution no longer blocks important stories from future runs.

2. Replace array-based item linkage with relational join table.
- Problem: `briefings.item_ids UUID[]` breaks relational guarantees and query scalability.
- Action: add `briefing_items(briefing_id, news_item_id, position, role, created_at)` and migrate reads/writes.
- Exit criteria: all selection/distribution/history logic uses join table; `item_ids` removed or deprecated.

3. Add missing `news_items` indexes for hot paths.
- Action: add composite indexes for analysis/writer claim queries:
  - `(status, updated_at, published_at DESC)`
  - `(status, relevance_score DESC, published_at DESC)`
  - `(published_at DESC)`
  - partial indexes for `status IN ('NEW','ANALYZING','ANALYZED','WRITING')` where useful.
- Exit criteria: explain plans show index usage; claim/query latency remains stable at larger volumes.

## Phase 1: Workflow Reliability + Operability

4. Introduce explicit retry state model.
- Problem: retries are implicit state flips and can loop forever.
- Action: add retry metadata (`retry_count`, `last_error`, `next_retry_at`, `terminal_status`) per pipeline stage.
- Exit criteria: poisoned items/drafts reach terminal states and are inspectable/recoverable.

5. Make handoff deterministic (no regex parsing from free text).
- Problem: writer→distributor depends on UUID extraction from message text.
- Action: return structured handoff payload (`briefing_id`, `mode`, `decision`) through explicit API contract.
- Exit criteria: orchestration does not depend on natural-language output.

6. Preserve distribution attempt history.
- Problem: per-channel outcomes are overwritten.
- Action: add `distribution_attempts` table (append-only) plus materialized/latest view for current state.
- Exit criteria: retries, transient failures, and recovery are auditable.

7. Move to migration-based schema lifecycle.
- Problem: runtime DDL in app code increases schema drift risk.
- Action: introduce migration tool (Alembic or equivalent), remove runtime `CREATE TABLE/INDEX` from hot paths.
- Exit criteria: schema changes happen only via versioned migrations in CI/CD.

## Phase 1.5: LLM Rate-Limit Stability (Highest Priority)

8. Tune retry/backoff limits for Vertex/Gemini calls.
- Problem: repeated `429 Too Many Requests` during write/critic/verifier loops causes long stalls and noisy retries.
- Action: change limits exactly as follows:
  - `BCN_LLM_CHAT_RETRIES`: `16` -> `10`
  - `BCN_LLM_RETRY_MAX_WAIT_SECONDS`: `600` -> `180`
  - `BCN_LLM_RETRY_JITTER_MIN_SECONDS`: `0.5` -> `1.0`
  - `BCN_LLM_RETRY_JITTER_MAX_SECONDS`: `5.0` -> `8.0`
  - introduce `BCN_LLM_RETRY_BASE_SECONDS=4.0`
  - introduce `BCN_LLM_RETRY_EXPONENT=1.7`
  - backoff formula: `wait = min(max_wait, base * exponent^(attempt-1)) + jitter`
  - keep `Retry-After` as a hard lower bound when present.
- Exit criteria: median `429` retries per workflow <= 2, and no single retry sleep > 180s.

9. Introduce a global model throttle shared by all roles.
- Problem: writer/critic/verifier/analyst can burst the same model endpoint concurrently.
- Action: add one in-process async throttle keyed by `(provider, base_url, model)` in `LLMClient`, used by all roles.
  - add `BCN_LLM_THROTTLE_ENABLED=true`
  - add `BCN_LLM_THROTTLE_MAX_IN_FLIGHT=1` (SBC default; raise to `2` only if stable)
  - add `BCN_LLM_THROTTLE_MIN_INTERVAL_MS=1200`
  - add `BCN_LLM_THROTTLE_429_COOLDOWN_SECONDS=20`
  - on `429`: set endpoint cooldown to `max(retry_after, cooldown_seconds)` and queue new requests until cooldown expires.
  - enforce FIFO queueing to avoid starving lower-priority stages.
  - add structured metrics/log fields: `llm_throttle_queue_wait_s`, `llm_in_flight`, `llm_429_count`, `llm_cooldown_s`.
- Exit criteria: no concurrent bursts above configured in-flight limit per model key; `429` count drops by >=50% under the same workload.

## Phase 2: Quality Gate Hardening for Engineer-Grade Output

10. Enforce model independence by role.
- Problem: writer/critic/verifier can default to same model endpoint.
- Action: require explicit role-level model/provider config in production; fail fast on missing critical role config.
- Exit criteria: critic/verifier are independent from writer in deployed environments.

11. Strengthen verifier pass semantics.
- Problem: verifier can pass despite weak LLM confidence.
- Action: pass condition should include score/confidence thresholds in addition to deterministic blockers.
- Exit criteria: low-confidence factual checks block publication.

12. Give critic/verifier richer evidence context.
- Problem: gates mostly see title/url/summary.
- Action: pass canonical source snippets, extracted claims, and key evidence artifacts into gate prompts.
- Exit criteria: measurable drop in factual-overreach and unsupported-claim issues.

13. Resolve prompt-vs-gate constraint conflicts.
- Problem: writer prompt and deterministic gates can disagree on structure/length.
- Action: unify envelopes (length, heading style, section requirements) under one shared config contract.
- Exit criteria: rewrite-loop churn decreases; first-pass acceptance rate improves.

## Phase 3: Throughput + Cost Efficiency

14. Reduce scraping overhead.
- Action: introduce shared fetcher pool/caching, use lightweight HTTP fetch first, Playwright only as fallback.
- Exit criteria: lower latency/cost per run without quality regression.

15. Remove blocking DNS operations from async path.
- Action: move host resolution checks to non-blocking flow (thread executor or async resolver), add TTL cache.
- Exit criteria: higher concurrency without event-loop stalls.

## Phase 4: Architecture + Scale

16. Split monolithic daemon into independently deployable services.
- Action: run collector, analyst, writer, critic, verifier, distributor, and scheduler as separate deploy units.
- Exit criteria: one service failure does not take down the full pipeline; horizontal scaling by stage is possible.

17. Add explicit workflow run ledger.
- Action: create `workflow_runs` and stage-attempt tables with idempotency keys, timestamps, and stage outcomes.
- Exit criteria: each daily/monthly cycle is traceable end-to-end with deterministic replay metadata.

## Delivery Sequence (Recommended)

1. Phase 1.5 (LLM rate-limit stability)
2. Phase 0 (correctness blockers)
3. Phase 1 (reliability and observability)
4. Phase 2 (quality hardening)
5. Phase 3 (performance/cost)
6. Phase 4 (service-level scaling)

## Validation Gates per Phase

1. Data correctness: no blocked stories from failed drafts; no duplicate publication from retries.
2. Reliability: bounded retries + terminal states + auditable attempts.
3. Quality: higher gate pass precision, fewer factual/link regressions.
4. Performance: stable claim/generation latency under increased corpus size.
5. Operability: independent service deploy/restart and clear incident blast radius.
