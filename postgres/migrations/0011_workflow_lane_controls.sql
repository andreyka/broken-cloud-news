CREATE TABLE IF NOT EXISTS workflow_lane_controls (
    lane VARCHAR(32) PRIMARY KEY,
    paused BOOLEAN NOT NULL DEFAULT FALSE,
    reason TEXT,
    updated_by VARCHAR(128),
    paused_at TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_workflow_lane_controls_paused
    ON workflow_lane_controls (paused)
    WHERE paused = TRUE;
