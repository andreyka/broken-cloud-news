# CLI Reference

The `bcn` console script is registered in `pyproject.toml` and loads command groups from `bcn/cli.py`.

## Core Workflow Commands

```bash
bcn collect --source all
bcn analyze
bcn write --mode regular_daily_briefing
bcn critique --latest
bcn verify --latest
bcn distribute --mode regular_daily_briefing
bcn pipeline --mode regular_daily_briefing
bcn workflow-run --mode ad_hoc
bcn run
```

Other core commands:

```bash
bcn db-migrate
bcn db-migrate --dry-run
bcn serve writer
bcn serve critic
bcn serve verifier
bcn serve collector
bcn serve analyst
bcn serve distributor
```

## Evaluation Commands

```bash
bcn simulate --limit 30
bcn simulate --since-days 7 --with-critic-rewrites
bcn benchmark-pack --output benchmark_pack.json
bcn benchmark --cases benchmark_pack.json
bcn shadow --candidate-overrides challenger.json --store-db
bcn evaluation-runs --lane shadow --limit 20
```

Semantics:

- `simulate`: historical replay against stored distributed briefings
- `benchmark-pack`: builds a curated benchmark input set from stored runs
- `benchmark`: champion vs challenger on a benchmark pack
- `shadow`: champion vs challenger on current upcoming items without publishing
- `evaluation-runs`: lists recent persisted benchmark/shadow runs

## Review And Training Commands

```bash
bcn review --decision accept
bcn review --decision edit --edited-file edited.md
bcn review-queue --only-unreviewed
bcn export-training --output-dir training_export
```

## Newsletter, History, And Admin Commands

```bash
bcn newsletter-subscribers list
bcn newsletter-subscribers add you@example.com
bcn newsletter-subscribers remove you@example.com
bcn import-history --file exports/telegram.txt --channel telegram
bcn record-outcome --briefing-id <uuid> --channel ghost --status ok --post-url https://example.com/post
bcn finalize-pending-runs --max-age-minutes 180 --decision blocked
```

These are the commands most likely to be missed if you only look at the old README:

- `newsletter-subscribers`
- `import-history`
- `record-outcome`
- `finalize-pending-runs`

## Optimization Commands

```bash
bcn optimize-run --variant optimization/variants/rewrite-budget-7.json
bcn optimize-runs --limit 20
```

See [optimization-loop.md](optimization-loop.md) for the optimization workflow.

## Notes

- `bcn run` is the scheduler/control-plane daemon used by the default Compose stack.
- `bcn serve <component>` runs one deployable service behind its HTTP contract.
- Most evaluation commands can persist results in PostgreSQL; the dashboard reads those persisted rows.
