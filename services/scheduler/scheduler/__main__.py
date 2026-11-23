"""Runs each job on its own interval in one loop."""

import asyncio
import logging
import os
import time

import asyncpg
import httpx

from . import jobs

EVERY = {  # seconds
    "deliver": 10,
    "weather": 1800,
    "calendars": 900,
    "goals": 3600,
    "digest": 3600,
    "retention": 86400,
}


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    log = logging.getLogger("muse.scheduler")
    pool = await asyncpg.create_pool(dsn=os.environ["MUSE_DATABASE_URL"], min_size=1, max_size=3)
    last: dict[str, float] = {}
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "MuseScheduler/0.1"}) as http:
        while True:
            now = time.monotonic()
            for name, every in EVERY.items():
                if now - last.get(name, -1e9) < every:
                    continue
                last[name] = now
                try:
                    async with pool.acquire() as conn:
                        fn = getattr(jobs, name)
                        result = await (fn(conn) if name in ("goals", "retention") else fn(conn, http))
                    if result:
                        log.info("%s: %s", name, result)
                except Exception:
                    log.exception("job %s failed", name)
            await asyncio.sleep(5)


asyncio.run(main())
