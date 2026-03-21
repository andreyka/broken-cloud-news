ALTER TABLE simulation_runs
    ADD COLUMN IF NOT EXISTS candidate_overrides JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE simulation_runs
    ADD COLUMN IF NOT EXISTS report JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE simulation_runs
    ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'completed';

ALTER TABLE simulation_runs
    ADD COLUMN IF NOT EXISTS finished_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE simulation_runs
    ADD COLUMN IF NOT EXISTS error_message TEXT;

UPDATE simulation_runs
SET status = 'completed'
WHERE status IS NULL OR status = '';

UPDATE simulation_runs
SET finished_at = COALESCE(finished_at, generated_at, updated_at, created_at)
WHERE status = 'completed' AND finished_at IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'simulation_runs_status_check'
    ) THEN
        ALTER TABLE simulation_runs
            ADD CONSTRAINT simulation_runs_status_check
            CHECK (status IN ('running', 'completed', 'failed', 'canceled'));
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS workflow_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    available_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    started_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE,
    deadline_at TIMESTAMP WITH TIME ZONE,
    lane VARCHAR(32) NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    job_type VARCHAR(64) NOT NULL,
    source VARCHAR(64) NOT NULL DEFAULT 'scheduler',
    workflow_id VARCHAR(64),
    dedupe_key TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    lease_duration_seconds INTEGER NOT NULL DEFAULT 900,
    lease_owner TEXT,
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    heartbeat_at TIMESTAMP WITH TIME ZONE,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    state JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    notes TEXT,
    CHECK (status IN ('queued', 'leased', 'completed', 'failed', 'canceled'))
);

CREATE INDEX IF NOT EXISTS idx_workflow_jobs_status_lane_priority_available
    ON workflow_jobs (status, lane, priority DESC, available_at ASC, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_workflow_jobs_lease_expires_at
    ON workflow_jobs (lease_expires_at)
    WHERE status = 'leased';
CREATE INDEX IF NOT EXISTS idx_workflow_jobs_deadline_at
    ON workflow_jobs (deadline_at)
    WHERE deadline_at IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_jobs_dedupe_active
    ON workflow_jobs (dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'leased');

CREATE TABLE IF NOT EXISTS workflow_job_attempts (
    id BIGSERIAL PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES workflow_jobs(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    worker_id TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'running',
    started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMP WITH TIME ZONE,
    error_message TEXT,
    state_before JSONB NOT NULL DEFAULT '{}'::jsonb,
    state_after JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (job_id, attempt_number),
    CHECK (status IN ('running', 'completed', 'failed', 'expired', 'canceled'))
);

CREATE INDEX IF NOT EXISTS idx_workflow_job_attempts_job_id_started_at
    ON workflow_job_attempts (job_id, started_at DESC);

CREATE TABLE IF NOT EXISTS workflow_job_artifacts (
    id BIGSERIAL PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES workflow_jobs(id) ON DELETE CASCADE,
    attempt_id BIGINT REFERENCES workflow_job_attempts(id) ON DELETE SET NULL,
    artifact_key VARCHAR(128) NOT NULL,
    artifact_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE (job_id, artifact_key)
);

CREATE INDEX IF NOT EXISTS idx_workflow_job_artifacts_job_id
    ON workflow_job_artifacts (job_id);
