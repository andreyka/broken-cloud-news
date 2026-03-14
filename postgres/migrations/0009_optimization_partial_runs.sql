ALTER TABLE optimization_candidate_lane_results
    ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'COMPLETED';

ALTER TABLE optimization_candidate_lane_results
    ADD COLUMN IF NOT EXISTS error_text TEXT;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'optimization_runs_status_check'
          AND conrelid = 'optimization_runs'::regclass
    ) THEN
        ALTER TABLE optimization_runs DROP CONSTRAINT optimization_runs_status_check;
    END IF;
END $$;

ALTER TABLE optimization_runs
    ADD CONSTRAINT optimization_runs_status_check
    CHECK (status IN ('PENDING', 'COMPLETED', 'FAILED', 'PARTIAL'));

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'optimization_candidates_status_check'
          AND conrelid = 'optimization_candidates'::regclass
    ) THEN
        ALTER TABLE optimization_candidates DROP CONSTRAINT optimization_candidates_status_check;
    END IF;
END $$;

ALTER TABLE optimization_candidates
    ADD CONSTRAINT optimization_candidates_status_check
    CHECK (status IN ('PENDING', 'COMPLETED', 'FAILED', 'PARTIAL'));

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'optimization_candidate_lane_results_status_check'
          AND conrelid = 'optimization_candidate_lane_results'::regclass
    ) THEN
        ALTER TABLE optimization_candidate_lane_results
            DROP CONSTRAINT optimization_candidate_lane_results_status_check;
    END IF;
END $$;

ALTER TABLE optimization_candidate_lane_results
    ADD CONSTRAINT optimization_candidate_lane_results_status_check
    CHECK (status IN ('COMPLETED', 'FAILED'));
