ALTER TABLE evaluation_runs
    ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'completed';

ALTER TABLE evaluation_runs
    ADD COLUMN IF NOT EXISTS finished_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE evaluation_runs
    ADD COLUMN IF NOT EXISTS error_message TEXT;

UPDATE evaluation_runs
SET status = 'completed'
WHERE status IS NULL OR status = '';

UPDATE evaluation_runs
SET finished_at = COALESCE(finished_at, generated_at, updated_at, created_at)
WHERE status = 'completed' AND finished_at IS NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'evaluation_runs_status_check'
    ) THEN
        ALTER TABLE evaluation_runs
            ADD CONSTRAINT evaluation_runs_status_check
            CHECK (status IN ('running', 'completed', 'failed'));
    END IF;
END
$$;
