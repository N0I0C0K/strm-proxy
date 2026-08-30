from __future__ import annotations

import asyncio

import httpx

from strm_proxy.catalog import XlysCatalog
from strm_proxy.config import AppSettings
from strm_proxy.database import create_media_repository
from strm_proxy.library import MediaLibrary


async def main() -> None:
    settings = AppSettings.from_environment()
    repository = create_media_repository(settings.database_path)
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(
                settings.request_timeout_seconds,
                connect=settings.connect_timeout_seconds,
            ),
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Encoding": "identity",
            },
        ) as client:
            catalog = XlysCatalog(
                client,
                recent_limit=settings.discovery_recent_limit,
                year_span=settings.discovery_year_span,
                movie_rating_threshold=settings.douban_rating_threshold,
                series_rating_threshold=(
                    settings.series_douban_rating_threshold
                ),
                cache_ttl_seconds=settings.catalog_cache_ttl_seconds,
            )
            library = MediaLibrary(repository, catalog)
            movies, series = await asyncio.gather(
                catalog.discover_movies(),
                catalog.discover_series(),
            )
            print(
                f"Discovery complete: movies={len(movies)}, "
                f"series={len(series)}"
            )

            # The existing database is disposable test data. Delay the clear until
            # both discovery requests have completed so a network error is harmless.
            repository.clear_all_media()
            await library.import_discovery(movies, series)

        stored_movies = repository.list_movies()
        stored_series = repository.list_visible_series()
        episode_count = sum(
            len(repository.list_episodes(item.xlys_id))
            for item in stored_series
        )
        print(
            f"Import complete: movies={len(stored_movies)}, "
            f"series={len(stored_series)}, episodes={episode_count}"
        )
    finally:
        repository.close()


if __name__ == "__main__":
    asyncio.run(main())
