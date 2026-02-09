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
    juiciness_score INTEGER,
    ai_tags JSONB,
    
    -- processing state
    status VARCHAR(20) DEFAULT 'NEW', -- NEW, SCRAPED, ANALYZED, PUBLISHED, DISCARDED
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    UNIQUE(source_type, source_id)
);
