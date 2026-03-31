ALTER TABLE generation_runs
    ADD COLUMN IF NOT EXISTS selection_trace JSONB NOT NULL DEFAULT '{}'::jsonb;
