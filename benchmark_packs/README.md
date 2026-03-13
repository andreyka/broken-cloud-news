# Benchmark Packs

Benchmark packs are fixed evaluation case sets used by the optimization loop.

Phase 1 can run against:
- a curated pack committed in this directory
- or an auto-built pack generated from stored runs and reviews

Current curated pack:
- `core_v1.json`
  - built from live SBC generation history
  - focuses on repeat handling, factual overclaim, same-product/new-advisory
    separation, and clean publishable controls
  - intended as the first stable offline quality gate for prompt/policy variants

Recommended first curated pack contents:
- true repeat cases that must be dropped
- same-product but new-advisory cases that must survive
- factual-overclaim correction cases
- good publishable GHSA/RSS/news cases
- quiet-day skip cases

Recommended human review:
- verify `expected_decision`
- verify `issue_tags`
- spot-check `reference_markdown` on the highest-value cases
- add or remove cases only when the benchmark intent is wrong, not because the
  source data is imperfect

See `docs/optimization-loop.md` for the operating model.
