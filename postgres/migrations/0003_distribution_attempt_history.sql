CREATE TABLE IF NOT EXISTS distribution_attempts (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    sent_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    briefing_id UUID NOT NULL REFERENCES briefings(id) ON DELETE CASCADE,
    channel VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    external_message_id TEXT,
    external_post_url TEXT,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_distribution_attempts_briefing_channel_sent
    ON distribution_attempts (briefing_id, channel, sent_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_distribution_attempts_briefing_id
    ON distribution_attempts (briefing_id);
CREATE INDEX IF NOT EXISTS idx_distribution_attempts_sent_at
    ON distribution_attempts (sent_at DESC);

INSERT INTO distribution_attempts (
    created_at,
    updated_at,
    sent_at,
    briefing_id,
    channel,
    status,
    external_message_id,
    external_post_url,
    metrics,
    metadata
)
SELECT
    COALESCE(o.created_at, NOW()) AS created_at,
    COALESCE(o.updated_at, NOW()) AS updated_at,
    COALESCE(o.sent_at, o.updated_at, o.created_at, NOW()) AS sent_at,
    o.briefing_id,
    o.channel,
    o.status,
    o.external_message_id,
    o.external_post_url,
    COALESCE(o.metrics, '{}'::jsonb) AS metrics,
    COALESCE(o.metadata, '{}'::jsonb) AS metadata
FROM briefing_distribution_outcomes o
WHERE NOT EXISTS (
    SELECT 1
    FROM distribution_attempts a
    WHERE a.briefing_id = o.briefing_id
      AND a.channel = o.channel
      AND a.status = o.status
      AND a.sent_at = COALESCE(o.sent_at, o.updated_at, o.created_at, NOW())
);

CREATE OR REPLACE VIEW distribution_outcomes_latest AS
SELECT DISTINCT ON (a.briefing_id, a.channel)
    a.id,
    a.created_at,
    a.updated_at,
    a.sent_at,
    a.briefing_id,
    a.channel,
    a.status,
    a.external_message_id,
    a.external_post_url,
    a.metrics,
    a.metadata
FROM distribution_attempts a
ORDER BY a.briefing_id, a.channel, a.sent_at DESC, a.id DESC;
