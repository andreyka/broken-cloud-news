CREATE TABLE IF NOT EXISTS briefing_ai_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    briefing_id UUID NOT NULL REFERENCES briefings(id) ON DELETE CASCADE,
    run_id UUID REFERENCES generation_runs(id) ON DELETE SET NULL,
    source VARCHAR(32) NOT NULL DEFAULT 'dashboard',
    reviewer_provider VARCHAR(32) NOT NULL DEFAULT 'openai',
    reviewer_model VARCHAR(128) NOT NULL,
    reasoning_effort VARCHAR(16),
    decision VARCHAR(16) NOT NULL,
    issue_tags TEXT[] NOT NULL DEFAULT '{}'::text[],
    edited_markdown TEXT,
    notes TEXT,
    raw_response JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (decision IN ('accept', 'reject', 'edit', 'needs_work'))
);

CREATE INDEX IF NOT EXISTS idx_briefing_ai_reviews_briefing_id
    ON briefing_ai_reviews (briefing_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_briefing_ai_reviews_run_id
    ON briefing_ai_reviews (run_id, created_at DESC);
