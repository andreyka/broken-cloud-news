# NEXT STEPS

## Priorities

1. Briefing quality comes first.
2. Stability comes second.
3. Local-model training readiness comes third.
4. Architecture and scale come fourth.

## Operating Rules

- No quality regression. Do not ship prompt, model, ranking, or architecture changes unless they match or beat the current production bar.
- Prefer evidence, typed contracts, and durable workflow state over more complexity.
- Favor a modular monolith first. Split services only when isolation or scale clearly requires it.
- Local-model work should run in shadow mode before it affects production publishing.

## What We Already Have

- `briefing_items` join table and migration-based schema lifecycle are in place.
- Retry state exists for both `news_items` and `briefings`.
- Distribution attempts are append-only and auditable.
- Writer to distributor handoff is structured, not regex-driven.
- Writer traces already persist runs, rounds, and preference pairs.

## Phase 1: Protect and Raise Briefing Quality

1. Add a quality release gate.
- Build a benchmark set from recent briefings and label each as `accept`, `edit`, or `reject`.
- Track why each draft passed or failed: factuality, novelty, operator value, link hygiene, and tone.
- Run champion/challenger evaluation before any production change.

2. Build evidence packs for selected items.
- Persist canonical source URL, supporting snippets, extracted claims, impacted product or version, exploit preconditions, blast radius, operator actions, and confidence.
- Feed the same evidence pack to writer, critic, verifier, and training export.

3. Unify the editorial contract.
- One shared contract should define length, structure, novelty rules, link rules, tone, and required operator guidance.
- Remove prompt versus gate mismatches.

4. Harden factual verification.
- Block low-confidence drafts, not only deterministic hard failures.
- Give critic and verifier richer evidence than just title, URL, and summary.

Exit criteria:
- No drop in current briefing quality.
- Higher first-pass acceptance rate.
- Fewer rewrite loops caused by prompt and gate disagreement.

## Phase 2: Make Publishing Reliable and Easy to Debug

1. Add a full workflow run ledger.
- Track collect, analyze, write, verify, and distribute attempts with timestamps, state transitions, idempotency keys, and errors.
- One query should explain why a day succeeded, blocked, retried, or failed.

2. Add shared LLM throttling and bounded retries.
- Use one in-process throttle keyed by provider, base URL, and model.
- Add FIFO queueing, cooldown on `429`, bounded backoff, and clear metrics.

3. Add deadline-aware safe mode.
- If the full creative pipeline stalls, publish a shorter pre-approved operator bulletin instead of missing the day.
- Safe mode must be quality-vetted and deterministic.

4. Tighten distribution recovery.
- Keep per-channel replay and dedupe deterministic.
- Separate quality blocks from infrastructure failures.

Exit criteria:
- No silent missed briefing days.
- Retries are bounded and auditable.
- Partial channel failures recover cleanly.

## Phase 3: Turn Production Into a Training-Data Factory

1. Expand stored artifacts.
- Persist selected items, ranking context, evidence packs, story cards or outlines, drafts, rewrites, critique output, verifier output, final briefing, human edits, and distribution outcomes.

2. Version the export format for training.
- Keep stable schemas for SFT and preference datasets.
- Treat human-accepted or quality-passed finals as gold data.

3. Start local-model shadow runs.
- Use local models for subtasks first: evidence extraction, story cards, rewrite assistance, critique, and verification.
- Keep full end-to-end local briefing generation in shadow mode until it consistently matches production quality.

Exit criteria:
- Enough clean accepted data to fine-tune a local model.
- A local challenger can run every day without affecting production.

## Phase 4: Simplify and Modularize the System

1. Refactor toward a modular monolith first.
- Keep one codebase and one durable Postgres workflow model.
- Split domain boundaries clearly: ingest, evidence, ranking, compose, critique, verify, distribute, and training export.

2. Split deployables only where it buys real value.
- Collector and scraper first.
- Generation and gating second.
- Distribution third.
- Keep A2A only where remote deployment or independent ownership justifies it.

3. Reduce scrape and network overhead.
- Try lightweight HTTP fetch first and use Playwright as fallback.
- Remove blocking DNS from async hot paths and add caching.

Exit criteria:
- Easier operations and debugging.
- Stage-level scaling is possible.
- Architecture is simpler to reason about, not more complex.

## Recommended Order

1. Quality release gate, evidence packs, and shared editorial contract.
2. LLM throttling, workflow ledger, safe mode, and recovery hardening.
3. Training-data completion and local shadow runs.
4. Modular refactor and selective service split.

## Not Now

- Do not switch production away from Gemini until a local challenger proves itself in shadow mode.
- Do not split into many services before workflow tracing, retries, and observability are solid.
- Do not add architecture complexity unless it improves quality, reliability, or operating clarity.
