# Evaluation Lanes

The `bcn.evaluation` package owns all non-publishing evaluation flows.

## Lanes

### Replay (`simulate`)

Historical replay regenerates briefings from previously distributed item sets
and compares the simulated output against what was actually published.

Use it to:
- detect regressions across a wider historical window
- compare the latest replay run with the previous replay baseline
- track long-range quality drift

Entry points:
- core logic: `bcn.evaluation.simulation`
- service orchestration: `bcn.evaluation.service.execute_simulation_lane`
- CLI: `bcn simulate`

### Benchmark (`benchmark-pack`, `benchmark`)

Benchmarking runs champion and challenger against a curated pack built from
stored runs, reviews, and published history.

Use it to:
- gate prompt/model/config promotions
- measure quality against fixed reviewed cases
- avoid shipping regressions masked by replay survivor bias

Entry points:
- pack + lane logic: `bcn.evaluation.lanes`
- service orchestration: `bcn.evaluation.service`
- CLI: `bcn benchmark-pack`, `bcn benchmark`

### Shadow (`shadow`)

Shadow compares champion and challenger on the current analyzed item pool
without publishing anything.

Use it to:
- observe real-day divergence before promotion
- collect daily challenger traces for future tuning or local-model training
- monitor recommendation trends in the dashboard

Entry points:
- lane logic: `bcn.evaluation.lanes`
- service orchestration: `bcn.evaluation.service.execute_shadow_lane`
- scheduler job: `bcn.workflows.automation.job_shadow_regular_briefing`
- CLI: `bcn shadow`

## Structure

`bcn.evaluation.lanes`
- benchmark and shadow comparison logic
- report construction and summaries

`bcn.evaluation.simulation`
- replay scoring and historical comparison logic

`bcn.evaluation.service`
- pool lifecycle
- DB persistence
- run status transitions
- JSON report writing

This keeps `bcn/cli.py` as a thin client: parse flags, call a service
function, print a small summary.

## Dashboard / Storage

Benchmark and shadow runs are persisted in `evaluation_runs`.
Replay runs are persisted in `simulation_runs`.

The Next.js dashboard reads those tables to show:
- latest lane outcomes
- running / failed / completed state
- per-run detail pages

## Recommended Operating Model

- `shadow`: scheduled daily, advisory only
- `simulate`: run regularly as a replay batch
- `benchmark`: run before promoting a challenger
- `benchmark-pack`: refresh periodically, not daily
