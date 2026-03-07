CREATE TABLE IF NOT EXISTS evaluation_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    generated_at TIMESTAMP WITH TIME ZONE,
    lane VARCHAR(32) NOT NULL,
    source VARCHAR(64) NOT NULL DEFAULT 'cli',
    report_path TEXT,
    pack_path TEXT,
    workflow_mode VARCHAR(32),
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    candidate_overrides JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    report JSONB NOT NULL DEFAULT '{}'::jsonb,
    count INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    CHECK (lane IN ('benchmark', 'shadow'))
);

CREATE INDEX IF NOT EXISTS idx_evaluation_runs_created_at
    ON evaluation_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_evaluation_runs_lane_created_at
    ON evaluation_runs (lane, created_at DESC);
