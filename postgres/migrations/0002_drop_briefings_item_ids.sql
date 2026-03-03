DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'briefings'
          AND column_name = 'item_ids'
    ) THEN
        INSERT INTO briefing_items (briefing_id, news_item_id, position, role, created_at)
        SELECT
            b.id,
            u.news_item_id,
            (u.ordinality - 1)::int AS position,
            'selected',
            COALESCE(b.created_at, NOW())
        FROM briefings b
        CROSS JOIN LATERAL UNNEST(COALESCE(b.item_ids, '{}'::uuid[]))
            WITH ORDINALITY AS u(news_item_id, ordinality)
        ON CONFLICT (briefing_id, news_item_id) DO NOTHING;

        ALTER TABLE briefings DROP COLUMN item_ids;
    END IF;
END
$$;
