import asyncio
import sys

from bcn.common.config import Settings
from bcn.common.db import get_pool


async def get_briefing_text(run_id: str):
    settings = Settings()
    pool = await get_pool(settings)
    async with pool.acquire() as conn:
        record = await conn.fetchrow(
            """
            SELECT final_draft
            FROM generation_runs
            WHERE id = $1
            """,
            run_id,
        )
        if record:
            print(record["final_draft"])
        else:
            print("Run not found")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        asyncio.run(get_briefing_text(sys.argv[1]))
    else:
        print("Provide run_id")
