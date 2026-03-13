# Benchmark Packs

Benchmark packs are fixed evaluation case sets used by the optimization loop.

Phase 1 can run against:
- a curated pack committed in this directory
- or an auto-built pack generated from stored runs and reviews

Recommended first curated pack contents:
- true repeat cases that must be dropped
- same-product but new-advisory cases that must survive
- factual-overclaim correction cases
- good publishable GHSA/RSS/news cases
- quiet-day skip cases

See `docs/optimization-loop.md` for the operating model.

