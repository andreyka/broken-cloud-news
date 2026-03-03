from __future__ import annotations

from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_get_analyzed_items_excludes_only_distributed_briefings():
    from bcn.common.db import get_analyzed_items

    fake_pool = AsyncMock()
    fake_pool.fetch = AsyncMock(return_value=[])

    with patch("bcn.common.db.get_pool", new_callable=AsyncMock) as mock_get_pool:
        mock_get_pool.return_value = fake_pool
        await get_analyzed_items(
            min_score=8,
            hours=12,
            limit=99,
            stale_writing_minutes=30,
        )

    args, _kwargs = fake_pool.fetch.await_args
    sql = args[0]

    assert "FROM briefings" in sql
    assert "news_items.id = ANY(briefings.item_ids)" in sql
    assert "briefings.status = 'DISTRIBUTED'" in sql
