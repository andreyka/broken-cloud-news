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

CREATE TABLE IF NOT EXISTS generation_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    trigger_source VARCHAR(64) NOT NULL DEFAULT 'writer',
    mode VARCHAR(32) NOT NULL DEFAULT 'standard',
    decision VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    decision_reason TEXT,
    rewrite_count INTEGER NOT NULL DEFAULT 0,
    briefing_id UUID REFERENCES briefings(id) ON DELETE SET NULL,
    selected_item_ids UUID[] NOT NULL DEFAULT '{}'::uuid[],
    selected_items JSONB NOT NULL DEFAULT '[]'::jsonb,
    llm_model TEXT,
    llm_model_version TEXT,
    prompts JSONB NOT NULL DEFAULT '{}'::jsonb,
    config_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    git_sha VARCHAR(128),
    initial_draft TEXT,
    final_draft TEXT,
    final_gate JSONB NOT NULL DEFAULT '{}'::jsonb,
    final_critique JSONB NOT NULL DEFAULT '{}'::jsonb,
    final_verifier JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_generation_runs_created_at
    ON generation_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_generation_runs_briefing_id
    ON generation_runs (briefing_id);

CREATE TABLE IF NOT EXISTS generation_rounds (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES generation_runs(id) ON DELETE CASCADE,
    round_index INTEGER NOT NULL,
    phase VARCHAR(32) NOT NULL DEFAULT 'initial',
    draft_input TEXT,
    gate_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    critique_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    verifier_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    feedback JSONB NOT NULL DEFAULT '[]'::jsonb,
    rewrite_output TEXT,
    passed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE (run_id, round_index)
);

CREATE INDEX IF NOT EXISTS idx_generation_rounds_run_id
    ON generation_rounds (run_id);

CREATE TABLE IF NOT EXISTS generation_preference_pairs (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES generation_runs(id) ON DELETE CASCADE,
    round_index INTEGER NOT NULL DEFAULT 0,
    source VARCHAR(64) NOT NULL DEFAULT 'auto_writer_loop',
    chosen_text TEXT NOT NULL,
    rejected_text TEXT NOT NULL,
    rationale TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_generation_preference_pairs_run_id
    ON generation_preference_pairs (run_id);

CREATE TABLE IF NOT EXISTS briefing_human_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    briefing_id UUID NOT NULL REFERENCES briefings(id) ON DELETE CASCADE,
    run_id UUID REFERENCES generation_runs(id) ON DELETE SET NULL,
    reviewer VARCHAR(128) NOT NULL DEFAULT 'cli',
    decision VARCHAR(16) NOT NULL,
    issue_tags TEXT[] NOT NULL DEFAULT '{}'::text[],
    edited_markdown TEXT,
    notes TEXT,
    CHECK (decision IN ('accept', 'reject', 'edit', 'needs_work'))
);

CREATE INDEX IF NOT EXISTS idx_briefing_human_reviews_briefing_id
    ON briefing_human_reviews (briefing_id, created_at DESC);

CREATE TABLE IF NOT EXISTS briefing_distribution_outcomes (
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
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (briefing_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_distribution_outcomes_briefing_id
    ON briefing_distribution_outcomes (briefing_id);
