# Next Steps (Future Sessions)

Last updated: 2026-02-22

## Fine-Tuning Readiness

Current DB data is useful for analysis and replay, but not yet enough for high-quality fine-tuning.

### What we already have
- `news_items`: source payloads, scraped content, summaries, relevance, tags.
- `briefings`: final output markdown/html and linked `item_ids`.
- `simulation_runs` / `simulation_results`: replay metrics, score deltas, per-briefing comparisons.

### What is missing (must add first)
1. Full generation traces per attempt:
   - prompt/system prompt version
   - model name/version
   - draft output
   - critique output
   - verifier output
   - rewrite outputs per round
   - final publish/block decision
2. Preference-pair data for ranking/DPO:
   - `chosen` vs `rejected` draft pairs with rationale
3. Human feedback labels:
   - edited text
   - accept/reject
   - issue tags (style, actionability, links, diversity, factuality)
4. Distribution outcomes linked to briefing:
   - post-level reactions/views/clicks
   - optional per-link click signals
5. Reproducibility metadata:
   - collector/analyzer/writer/critic/verifier config snapshot
   - git commit SHA of code used

## Recommended implementation order
1. Add DB tables for run traces and attempt-level artifacts.
2. Persist writer->critic->verifier loop artifacts per rewrite round.
3. Add simple human-review table and CLI to label/edit.
4. Store distribution outcome metrics (Telegram engagement) by `briefing_id`.
5. Build export job to produce JSONL datasets for SFT + preference training.

## Decision rule before fine-tuning
Do **not** start fine-tuning until at least:
- 2-3 weeks of trace data,
- stable quality gates,
- enough labeled pairs (accepted vs rejected drafts),
- engagement/outcome signals tied to generated posts.
