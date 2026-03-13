CREATE TABLE IF NOT EXISTS optimization_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL DEFAULT 'cli',
    git_sha TEXT,
    benchmark_pack_path TEXT,
    replay_limit INTEGER NOT NULL DEFAULT 20,
    replay_since_days INTEGER NOT NULL DEFAULT 60,
    notes TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CHECK (status IN ('PENDING', 'COMPLETED', 'FAILED'))
);

CREATE TABLE IF NOT EXISTS optimization_candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    optimization_run_id UUID NOT NULL REFERENCES optimization_runs(id) ON DELETE CASCADE,
    variant_id TEXT NOT NULL,
    base_variant TEXT NOT NULL DEFAULT 'champion',
    variant_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    hard_reject BOOLEAN NOT NULL DEFAULT FALSE,
    recommendation TEXT,
    composite_score DOUBLE PRECISION,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CHECK (status IN ('PENDING', 'COMPLETED', 'FAILED'))
);

CREATE INDEX IF NOT EXISTS idx_optimization_candidates_run_created
    ON optimization_candidates (optimization_run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS optimization_candidate_lane_results (
    id BIGSERIAL PRIMARY KEY,
    optimization_candidate_id UUID NOT NULL REFERENCES optimization_candidates(id) ON DELETE CASCADE,
    lane TEXT NOT NULL,
    report JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    hard_reject BOOLEAN NOT NULL DEFAULT FALSE,
    score DOUBLE PRECISION,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_optimization_candidate_lane_results_candidate_lane
    ON optimization_candidate_lane_results (optimization_candidate_id, lane, created_at DESC);
