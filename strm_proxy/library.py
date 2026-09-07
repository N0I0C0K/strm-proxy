from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import httpx

from .catalog import CatalogEntry, XlysCatalog, safe_media_name
from .database import Episode, MediaItem, MediaRepository
from .detail import XlysDetail, fetch_xlys_detail
from .logging_utils import describe_http_error, safe_url_for_log
from .models import ResolverError


logger = logging.getLogger(__name__)


class MediaLibrary:
    """Persist discovery results and expose database-backed DAV views."""

    def __init__(
        self,
        repository: MediaRepository,
        catalog: XlysCatalog,
        *,
        detail_concurrency: int = 3,
        detail_request_interval_seconds: float = 0.25,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.recent_limit = catalog.recent_limit
        self._detail_concurrency = detail_concurrency
        self._detail_request_interval_seconds = detail_request_interval_seconds
        self._movie_bootstrap_lock = asyncio.Lock()
        self._series_bootstrap_lock = asyncio.Lock()

    async def visible_movies(self) -> tuple[MediaItem, ...]:
        await self._bootstrap_movies_if_empty()
        return self.repository.list_visible_movies()

    async def find_visible_movie_by_filename(
        self,
        filename: str,
    ) -> MediaItem | None:
        return next(
            (
                movie
                for movie in await self.visible_movies()
                if movie.dav_name == filename
            ),
            None,
        )

    async def visible_series(self) -> tuple[MediaItem, ...]:
        await self._bootstrap_series_if_empty()
        return self.repository.list_visible_series()

    async def find_visible_series_by_name(
        self,
        dav_name: str,
    ) -> MediaItem | None:
        await self._bootstrap_series_if_empty()
        return self.repository.find_visible_series_by_dav_name(dav_name)

    def episodes(self, series: MediaItem) -> tuple[Episode, ...]:
        return self.repository.list_episodes(series.xlys_id)

    async def sync_from_source(self) -> tuple[MediaItem, ...]:
        logger.info("event=library_sync_start kind=all")
        movies, series_entries = await asyncio.gather(
            self.catalog.discover_movies(),
            self.catalog.discover_series(),
        )
        await self.import_discovery(movies, series_entries)
        visible_movies = self.repository.list_visible_movies()
        logger.info(
            "event=library_sync_complete movies=%d series=%d",
            len(visible_movies),
            len(self.repository.list_visible_series()),
        )
        return visible_movies

    async def import_discovery(
        self,
        movies: tuple[CatalogEntry, ...],
        series_entries: tuple[CatalogEntry, ...],
    ) -> None:
        """Persist a completed discovery snapshot as the new automatic catalog."""
        series_details = await self._fetch_series_details(
            tuple(
                entry
                for entry in series_entries
                if entry.available_episode_count is None
            )
        )
        logger.info(
            "event=library_import_discovery movies=%d series=%d "
            "series_details=%d",
            len(movies),
            len(series_entries),
            len(series_details),
        )
        self.repository.import_discovered_movies(movies, replace_auto=True)
        self.repository.import_discovered_series(
            series_entries,
            series_details,
            replace_auto=True,
        )

    async def sync_movies_from_source(self) -> tuple[MediaItem, ...]:
        logger.info("event=library_sync_start kind=movie")
        discovered = await self.catalog.discover_movies()
        self.repository.import_discovered_movies(discovered, replace_auto=True)
        visible = self.repository.list_visible_movies()
        logger.info(
            "event=library_sync_complete kind=movie discovered=%d visible=%d",
            len(discovered),
            len(visible),
        )
        return visible

    async def sync_series_from_source(self) -> tuple[MediaItem, ...]:
        logger.info("event=library_sync_start kind=series")
        entries = await self.catalog.discover_series()
        details = await self._fetch_series_details(
            tuple(
                entry
                for entry in entries
                if entry.available_episode_count is None
            )
        )
        self.repository.import_discovered_series(
            entries,
            details,
            replace_auto=True,
        )
        visible = self.repository.list_visible_series()
        logger.info(
            "event=library_sync_complete kind=series discovered=%d visible=%d",
            len(entries),
            len(visible),
        )
        return visible

    def movie_play_url(self, movie: MediaItem) -> str:
        return f"{self.catalog.origin}/play/{movie.xlys_id}-0.htm"

    def series_season_directory(self, series: MediaItem) -> str:
        return f"Season {(series.season_number or 1):02d}"

    def episode_filename(self, series: MediaItem, episode: Episode) -> str:
        title = safe_media_name(series.title, None)
        season = series.season_number or 1
        number = episode.source_index + 1
        return f"{title} S{season:02d}E{number:02d}.strm"

    def episode_play_url(self, series: MediaItem, episode: Episode) -> str:
        return f"{self.catalog.origin}{episode.play_path}"

    def find_episode_by_filename(
        self,
        series: MediaItem,
        filename: str,
    ) -> Episode | None:
        return next(
            (
                episode
                for episode in self.episodes(series)
                if self.episode_filename(series, episode) == filename
            ),
            None,
        )

    async def _bootstrap_movies_if_empty(self) -> None:
        if self.repository.has_movies():
            return
        async with self._movie_bootstrap_lock:
            if not self.repository.has_movies():
                logger.info("event=library_bootstrap kind=movie")
                await self.sync_movies_from_source()

    async def _bootstrap_series_if_empty(self) -> None:
        if self.repository.has_series():
            return
        async with self._series_bootstrap_lock:
            if not self.repository.has_series():
                logger.info("event=library_bootstrap kind=series")
                await self.sync_series_from_source()

    async def _fetch_series_details(
        self,
        entries: tuple[CatalogEntry, ...],
    ) -> tuple[XlysDetail, ...]:
        semaphore = asyncio.Semaphore(self._detail_concurrency)
        pace_lock = asyncio.Lock()
        last_request_at = 0.0
        host = urlparse(self.catalog.origin).hostname
        allowed_hosts = (host,) if host else ()

        async def fetch(entry: CatalogEntry) -> XlysDetail | None:
            nonlocal last_request_at
            if entry.source_url is None:
                return None
            async with semaphore:
                async with pace_lock:
                    loop = asyncio.get_running_loop()
                    remaining = (
                        self._detail_request_interval_seconds
                        - (loop.time() - last_request_at)
                    )
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                    last_request_at = loop.time()
                try:
                    return await fetch_xlys_detail(
                        self.catalog.client,
                        entry.source_url,
                        allowed_hosts=allowed_hosts,
                    )
                except (httpx.HTTPError, ResolverError) as exc:
                    detail = (
                        describe_http_error(exc)
                        if isinstance(exc, httpx.HTTPError)
                        else str(exc)
                    )
                    logger.warning(
                        "event=series_detail_failed xlys_id=%d source=%s detail=%s",
                        entry.xlys_id,
                        safe_url_for_log(entry.source_url),
                        detail,
                    )
                    return None

        details = tuple(
            detail
            for detail in await asyncio.gather(*(fetch(entry) for entry in entries))
            if detail is not None and detail.kind == "series"
        )
        return details
