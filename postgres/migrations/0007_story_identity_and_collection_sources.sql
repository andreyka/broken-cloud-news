ALTER TABLE news_items
    ADD COLUMN IF NOT EXISTS story_url_key TEXT,
    ADD COLUMN IF NOT EXISTS story_issue_key TEXT;

CREATE INDEX IF NOT EXISTS idx_news_items_story_url_key
    ON news_items (story_url_key);

CREATE INDEX IF NOT EXISTS idx_news_items_story_issue_key
    ON news_items (story_issue_key);

CREATE TABLE IF NOT EXISTS collection_sources (
    source_key TEXT PRIMARY KEY,
    source_type VARCHAR(32) NOT NULL,
    display_name TEXT NOT NULL,
    state VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    raw_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    review_reason TEXT,
    review_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    discovered_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    first_active_at TIMESTAMP WITH TIME ZONE,
    last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CHECK (state IN ('ACTIVE', 'PENDING_REVIEW', 'QUARANTINED', 'DISABLED'))
);

CREATE INDEX IF NOT EXISTS idx_collection_sources_type_state
    ON collection_sources (source_type, state, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS collection_source_reviews (
    id BIGSERIAL PRIMARY KEY,
    source_key TEXT NOT NULL REFERENCES collection_sources(source_key) ON DELETE CASCADE,
    decision VARCHAR(20) NOT NULL,
    confidence VARCHAR(16) NOT NULL DEFAULT 'medium',
    rationale TEXT,
    review_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    CHECK (decision IN ('promote', 'quarantine')),
    CHECK (confidence IN ('low', 'medium', 'high'))
);

CREATE INDEX IF NOT EXISTS idx_collection_source_reviews_source_created
    ON collection_source_reviews (source_key, created_at DESC);
