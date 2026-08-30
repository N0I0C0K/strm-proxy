from __future__ import annotations

import asyncio

import httpx

from strm_proxy.catalog import XlysCatalog
from strm_proxy.config import DEFAULT_USER_AGENT


async def main() -> None:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Encoding": "identity",
        },
    ) as client:
        catalog = XlysCatalog(client, cache_ttl_seconds=60)
        movies, series = await asyncio.gather(
            catalog.discover_movies(),
            catalog.discover_series(),
        )
    for label, threshold, entries in (
        ("movies", 7.0, movies),
        ("series", 8.0, series),
    ):
        qualifying = tuple(
            entry.douban_rating
            for entry in entries
            if entry.douban_rating is not None
            and entry.douban_rating > threshold
        )
        print(
            label,
            f"total={len(entries)}",
            f"rated_above_{threshold:g}={len(qualifying)}",
            f"minimum_qualifying={min(qualifying, default=None)}",
            f"episode_counts={sum(entry.available_episode_count is not None for entry in entries)}",
        )


if __name__ == "__main__":
    asyncio.run(main())
