from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse

import httpx

from .catalog import CatalogEntry, XlysCatalog, safe_media_name
from .database import CacheRepository, Episode, MediaItem, MediaRepository
from .detail import XlysDetail, fetch_xlys_detail
from .logging_utils import describe_http_error, safe_url_for_log
from .libvio_catalog import LibvioCatalog
from .models import ResolverError


logger = logging.getLogger(__name__)


Provider = Literal["xlys", "libvio"]


@dataclass(frozen=True, slots=True)
class ProviderSyncState:
    status: Literal["idle", "syncing", "success", "partial", "failed"] = "idle"
    last_synced_at: datetime | None = None
    failures: tuple[str, ...] = ()


class MediaLibrary:
    """Persist discovery results and expose database-backed DAV views."""

    def __init__(
        self,
        repository: MediaRepository,
        catalog: XlysCatalog,
        *,
        libvio_catalog: LibvioCatalog | None = None,
        detail_concurrency: int = 3,
        detail_request_interval_seconds: float = 0.25,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.libvio_catalog = libvio_catalog
        self.last_discovery_failures: tuple[str, ...] = ()
        self._provider_locks = {"xlys": asyncio.Lock(), "libvio": asyncio.Lock()}
        self._sync_history = CacheRepository(repository.engine)
        self.provider_sync_states = {source: self._load_sync_state(source) for source in ("xlys", "libvio")}
        self.recent_limit = catalog.recent_limit
        self._detail_concurrency = detail_concurrency
        self._detail_request_interval_seconds = detail_request_interval_seconds
        self._movie_bootstrap_lock = asyncio.Lock()
        self._series_bootstrap_lock = asyncio.Lock()

    def _load_sync_state(self, provider: str) -> ProviderSyncState:
        raw = self._sync_history.get(f"provider-sync:{provider}")
        if raw:
            try:
                value = json.loads(raw)
                if value["status"] in {"success", "partial", "failed"}:
                    failures = value["failures"]
                    if isinstance(failures, list) and all(isinstance(item, str) for item in failures):
                        return ProviderSyncState(value["status"], datetime.fromisoformat(value["last_synced_at"]),
                                                 tuple(failures))
            except (ValueError, KeyError, TypeError):
                pass
        return ProviderSyncState()

    def _finish_sync(self, provider: str, state: ProviderSyncState) -> None:
        self.provider_sync_states[provider] = state
        self._sync_history.set(f"provider-sync:{provider}", json.dumps({
            "status": state.status,
            "last_synced_at": state.last_synced_at.isoformat() if state.last_synced_at else None,
            "failures": state.failures,
        }, ensure_ascii=False))

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

    async def sync_from_source(self, *, provider: Provider | None = None) -> tuple[MediaItem, ...]:
        logger.info("event=library_sync_start kind=all provider=%s", provider or "all")
        await self._sync_kinds(("movie", "series"), provider=provider)
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
        await self._sync_kinds(("movie",))
        visible = self.repository.list_visible_movies()
        logger.info(
            "event=library_sync_complete kind=movie visible=%d",
            len(visible),
        )
        return visible

    async def sync_series_from_source(self) -> tuple[MediaItem, ...]:
        logger.info("event=library_sync_start kind=series")
        await self._sync_kinds(("series",))
        visible = self.repository.list_visible_series()
        logger.info(
            "event=library_sync_complete kind=series visible=%d",
            len(visible),
        )
        return visible

    async def _sync_kinds(self, kinds: tuple[str, ...], *, provider: Provider | None = None) -> None:
        async def sync_xlys(kind: str) -> tuple[str, ...]:
            if kind == "movie":
                entries = await self.catalog.discover_movies()
                self.repository.import_discovered_movies(entries, replace_auto=True)
            else:
                entries = await self.catalog.discover_series()
                details = await self._fetch_series_details(tuple(
                    entry for entry in entries if entry.available_episode_count is None
                ))
                self.repository.import_discovered_series(entries, details, replace_auto=True)
            return ()

        async def sync_libvio(kind: str) -> tuple[str, ...]:
            assert self.libvio_catalog is not None
            result = await (self.libvio_catalog.discover_movies() if kind == "movie"
                            else self.libvio_catalog.discover_series())
            self.repository.import_discovered_libvio(result.details, kind=kind,
                                                      replace_auto=result.complete)
            return result.failures

        available = ("xlys", "libvio") if self.libvio_catalog is not None else ("xlys",)
        if provider is not None and provider not in available:
            raise ResolverError("该供应商未启用")
        selected = (provider,) if provider is not None else available

        async def sync_provider(source: str) -> tuple[int, tuple[str, ...]]:
            async with self._provider_locks[source]:
                previous = self.provider_sync_states[source]
                self.provider_sync_states[source] = ProviderSyncState("syncing", previous.last_synced_at)
                name = "雪落影视" if source == "xlys" else "LIBVIO"
                sync = sync_xlys if source == "xlys" else sync_libvio
                results = await asyncio.gather(*(sync(kind) for kind in kinds), return_exceptions=True)
                failures: list[str] = []
                completed = 0
                for kind, result in zip(kinds, results):
                    if isinstance(result, (httpx.HTTPError, ResolverError)):
                        label = "电影" if kind == "movie" else "电视剧"
                        failures.append(f"{name}{label}发现失败，已保留原有条目")
                        logger.warning("event=library_discovery_failed provider=%s kind=%s", source, kind)
                    elif isinstance(result, BaseException):
                        self._finish_sync(source, ProviderSyncState(
                            "failed", datetime.now(timezone.utc), (f"{name}同步失败",),
                        ))
                        raise result
                    else:
                        completed += 1
                        failures.extend(result)
                status = "success" if not failures else "partial" if completed else "failed"
                self._finish_sync(source, ProviderSyncState(
                    status, datetime.now(timezone.utc), tuple(failures),
                ))
                return completed, tuple(failures)

        results = await asyncio.gather(*(sync_provider(source) for source in selected), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        self.last_discovery_failures = tuple(failure for _, failures in results for failure in failures)
        if not any(completed for completed, _ in results):
            raise ResolverError("；".join(self.last_discovery_failures))

    def movie_play_url(self, movie: MediaItem) -> str:
        if movie.provider == "libvio" and movie.source_url:
            return movie.source_url
        return f"{self.catalog.origin}/play/{movie.xlys_id}-0.htm"

    def series_season_directory(self, series: MediaItem) -> str:
        return f"Season {(series.season_number or 1):02d}"

    def episode_filename(self, series: MediaItem, episode: Episode) -> str:
        title = safe_media_name(series.title, None)
        season = series.season_number or 1
        number = episode.source_index + 1
        return f"{title} S{season:02d}E{number:02d}.strm"

    def episode_play_url(self, series: MediaItem, episode: Episode) -> str:
        if series.provider == "libvio" and series.source_url:
            parsed = urlparse(series.source_url)
            return f"{parsed.scheme}://{parsed.netloc}{episode.play_path}"
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
