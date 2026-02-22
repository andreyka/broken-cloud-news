CREATE TABLE IF NOT EXISTS news_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type VARCHAR(50) NOT NULL, -- 'ghsa', 'twitter', 'rss', 'cisa'
    source_id VARCHAR(255) NOT NULL, -- unique ID from source (TweetID, GHSA-ID, URL)
    url TEXT NOT NULL,
    title TEXT,
    published_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    -- raw_data stores the full original JSON from the source (Tweet object, RSS item)
    raw_data JSONB,

    -- content populated by Scraper
    full_content TEXT,

    -- analysis results
    summary TEXT,
    relevance_score INTEGER,
    ai_tags JSONB,
    image_prompt TEXT,
    image_url TEXT,

    -- processing state
    status VARCHAR(20) DEFAULT 'NEW', -- NEW, SCRAPED, ANALYZED, PUBLISHED, DISCARDED

    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    UNIQUE(source_type, source_id)
);

CREATE TABLE IF NOT EXISTS briefings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    cover_image_url TEXT,
    cover_image_prompt TEXT,
    content_markdown TEXT NOT NULL,
    content_html TEXT,
    item_ids UUID[] NOT NULL,
    status VARCHAR(20) DEFAULT 'DRAFT', -- DRAFT, DISTRIBUTED
    distributed_at TIMESTAMP WITH TIME ZONE,
    distribution_channels JSONB,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS simulation_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    generated_at TIMESTAMP WITH TIME ZONE,
    source VARCHAR(64) NOT NULL DEFAULT 'cli',
    report_path TEXT,
    params JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    count INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_simulation_runs_created_at
    ON simulation_runs (created_at DESC);

CREATE TABLE IF NOT EXISTS simulation_results (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES simulation_runs(id) ON DELETE CASCADE,
    briefing_id TEXT,
    briefing_created_at TIMESTAMP WITH TIME ZONE,
    actual_score INTEGER NOT NULL,
    simulated_score INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (run_id, briefing_id)
);

CREATE INDEX IF NOT EXISTS idx_simulation_results_run_id
    ON simulation_results (run_id);
CREATE INDEX IF NOT EXISTS idx_simulation_results_briefing_id
    ON simulation_results (briefing_id);
