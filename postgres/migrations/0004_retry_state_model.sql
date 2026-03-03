ALTER TABLE news_items
    ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE news_items
    ADD COLUMN IF NOT EXISTS last_error TEXT;

ALTER TABLE news_items
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE news_items
    ADD COLUMN IF NOT EXISTS terminal_status VARCHAR(64);

ALTER TABLE briefings
    ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE briefings
    ADD COLUMN IF NOT EXISTS last_error TEXT;

ALTER TABLE briefings
    ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE briefings
    ADD COLUMN IF NOT EXISTS terminal_status VARCHAR(64);

UPDATE news_items
SET terminal_status = NULL
WHERE terminal_status = '';

UPDATE briefings
SET terminal_status = NULL
WHERE terminal_status = '';

CREATE INDEX IF NOT EXISTS idx_news_items_retry_ready
    ON news_items (status, terminal_status, next_retry_at, updated_at);

CREATE INDEX IF NOT EXISTS idx_briefings_retry_ready
    ON briefings (status, terminal_status, next_retry_at, updated_at);
