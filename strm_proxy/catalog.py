from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from .cache import TTLCache
from .logging_utils import describe_http_error
from .models import ResolverError


_DETAIL_PATH_PATTERN = re.compile(r"^/[^/?#]+/(\d+)\.htm$")
_YEAR_PATTERN = re.compile(r"^\s*((?:19|20)\d{2})")
_RATING_PATTERN = re.compile(r"\d+(?:\.\d+)?")
_UPDATED_EPISODES_PATTERN = re.compile(r"(?:更至|更新至)\s*(\d+)\s*集")
_COMPLETE_EPISODES_PATTERN = re.compile(
    r"(?:全\s*(\d+)\s*集|(\d+)\s*集全)"
)
_INVALID_FILENAME_PATTERN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

logger = logging.getLogger(__name__)


def _media_type_label(media_type: int) -> str:
    return "series" if media_type == 1 else "movie"


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    xlys_id: int
    title: str
    year: int | None
    cover_url: str | None
    source_updated_on: str
    dav_filename: str
    source_url: str | None = None
    douban_rating: float | None = None
    available_episode_count: int | None = None
    declared_episode_count: int | None = None


class _MediaCardParser(HTMLParser):
    def __init__(self, origin: str) -> None:
        super().__init__(convert_charrefs=True)
        self.origin = origin
        self.entries: list[CatalogEntry] = []
        self._card_depth = 0
        self._card: dict[str, str] | None = None
        self._capture: str | None = None
        self._capture_tag: str | None = None
        self._capture_depth = 0
        self._text: list[str] = []
        self._meta_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "div" and "movie-card" in classes and self._card is None:
            self._card = {}
            self._card_depth = 1
            return
        if self._card is None:
            return
        if tag == "div":
            self._card_depth += 1
            if "card-meta" in classes:
                self._meta_depth = self._card_depth
            elif "rating-badge" in classes:
                self._start_capture("douban_rating", tag)
        if tag == "a" and "card-img" in classes:
            self._card["href"] = values.get("href") or ""
            self._card["description"] = values.get("title") or ""
        elif tag == "img" and "cover_url" not in self._card:
            self._card["cover_url"] = (
                values.get("data-src") or values.get("src") or ""
            )
        elif tag == "h4":
            self._start_capture("title", tag)
        elif tag == "span" and self._meta_depth:
            self._start_capture("updated_at", tag)

    def handle_endtag(self, tag: str) -> None:
        if self._card is None:
            return
        if (
            self._capture
            and self._capture_tag == tag
            and self._capture_depth == self._card_depth
        ):
            self._card[self._capture] = "".join(self._text).strip()
            self._capture = None
            self._capture_tag = None
            self._text = []
        if tag == "div":
            if self._meta_depth == self._card_depth:
                self._meta_depth = 0
            self._card_depth -= 1
            if self._card_depth == 0:
                entry = _entry_from_card(self._card, self.origin)
                if entry is not None:
                    self.entries.append(entry)
                self._card = None

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._text.append(data)

    def _start_capture(self, name: str, tag: str) -> None:
        self._capture = name
        self._capture_tag = tag
        self._capture_depth = self._card_depth
        self._text = []


def parse_catalog_page(html: str, origin: str) -> tuple[CatalogEntry, ...]:
    parser = _MediaCardParser(origin)
    parser.feed(html)
    parser.close()
    return tuple(parser.entries)


class XlysCatalog:
    """Discover movies and series from rating and recency collections."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        origin: str = "https://www.xlys02.com",
        recent_limit: int = 50,
        year_span: int = 4,
        movie_rating_threshold: float = 7.0,
        series_rating_threshold: float = 8.0,
        current_year: int | None = None,
        request_interval_seconds: float = 1.0,
        cache_ttl_seconds: float = 1800,
    ) -> None:
        self.client = client
        self.origin = origin.rstrip("/")
        self.recent_limit = recent_limit
        self.year_span = year_span
        self.movie_rating_threshold = movie_rating_threshold
        self.series_rating_threshold = series_rating_threshold
        self.current_year = current_year or date.today().year
        self.request_interval_seconds = request_interval_seconds
        self._cache = TTLCache[str, tuple[CatalogEntry, ...]](
            cache_ttl_seconds
        )
        self._last_good: dict[int, tuple[CatalogEntry, ...]] = {}
        self._refresh_locks = {0: asyncio.Lock(), 1: asyncio.Lock()}
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def discover_movies(self) -> tuple[CatalogEntry, ...]:
        return await self._discover(media_type=0)

    async def discover_series(self) -> tuple[CatalogEntry, ...]:
        return await self._discover(media_type=1)

    async def latest_movies(self) -> tuple[CatalogEntry, ...]:
        """Compatibility name for the movie discovery collection."""
        return await self.discover_movies()

    async def _discover(self, *, media_type: int) -> tuple[CatalogEntry, ...]:
        key = f"discovery:{media_type}"
        if cached := self._cache.get(key):
            logger.debug(
                "event=catalog_cache_hit kind=%s count=%d",
                _media_type_label(media_type),
                len(cached),
            )
            return cached
        async with self._refresh_locks[media_type]:
            if cached := self._cache.get(key):
                logger.debug(
                    "event=catalog_cache_hit kind=%s count=%d",
                    _media_type_label(media_type),
                    len(cached),
                )
                return cached
            logger.info(
                "event=catalog_discovery_start kind=%s recent_limit=%d "
                "year_span=%d",
                _media_type_label(media_type),
                self.recent_limit,
                self.year_span,
            )
            try:
                entries = await self._fetch_discovery(media_type)
            except (httpx.HTTPError, ResolverError) as exc:
                if previous := self._last_good.get(media_type):
                    detail = (
                        describe_http_error(exc)
                        if isinstance(exc, httpx.HTTPError)
                        else str(exc)
                    )
                    logger.warning(
                        "event=catalog_discovery_fallback kind=%s count=%d "
                        "detail=%s",
                        _media_type_label(media_type),
                        len(previous),
                        detail,
                    )
                    return previous
                raise
            self._cache.set(key, entries)
            self._last_good[media_type] = entries
            logger.info(
                "event=catalog_discovery_complete kind=%s count=%d",
                _media_type_label(media_type),
                len(entries),
            )
            return entries

    async def _fetch_discovery(
        self,
        media_type: int,
    ) -> tuple[CatalogEntry, ...]:
        years = range(self.current_year, self.current_year - self.year_span, -1)
        recent, *rated_years = await asyncio.gather(
            self._fetch_recent(media_type),
            *(self._fetch_rated_year(media_type, year) for year in years),
        )

        combined: list[CatalogEntry] = []
        seen_ids: set[int] = set()
        for entries in (*rated_years, recent):
            for entry in entries:
                if entry.xlys_id in seen_ids:
                    continue
                combined.append(entry)
                seen_ids.add(entry.xlys_id)
        if not combined:
            label = "series" if media_type == 1 else "movie"
            raise ResolverError(
                f"The xlys {label} discovery did not contain any media cards"
            )
        return _deduplicate_filenames(tuple(combined))

    async def _fetch_recent(self, media_type: int) -> tuple[CatalogEntry, ...]:
        entries: list[CatalogEntry] = []
        seen_ids: set[int] = set()
        page = 1
        while len(entries) < self.recent_limit:
            page_entries = await self._fetch_page(
                media_type=media_type,
                order=0,
                page=page,
            )
            if not page_entries:
                break
            previous_count = len(entries)
            for entry in page_entries:
                if entry.xlys_id in seen_ids:
                    continue
                entries.append(entry)
                seen_ids.add(entry.xlys_id)
                if len(entries) == self.recent_limit:
                    break
            if len(entries) == previous_count:
                break
            page += 1
        return tuple(entries)

    async def _fetch_rated_year(
        self,
        media_type: int,
        year: int,
    ) -> tuple[CatalogEntry, ...]:
        entries: list[CatalogEntry] = []
        seen_ids: set[int] = set()
        page = 1
        threshold = (
            self.series_rating_threshold
            if media_type == 1
            else self.movie_rating_threshold
        )
        while True:
            page_entries = await self._fetch_page(
                media_type=media_type,
                order=1,
                year=year,
                page=page,
            )
            if not page_entries:
                break
            above_threshold = tuple(
                entry
                for entry in page_entries
                if entry.douban_rating is not None
                and entry.douban_rating > threshold
            )
            qualifying = tuple(
                entry
                for entry in above_threshold
                if entry.year is not None
                and self.current_year - self.year_span < entry.year <= self.current_year
            )
            for entry in qualifying:
                if entry.xlys_id not in seen_ids:
                    entries.append(entry)
                    seen_ids.add(entry.xlys_id)
            if len(above_threshold) < len(page_entries):
                break
            page += 1
        return tuple(entries)

    async def _fetch_page(
        self,
        *,
        media_type: int,
        order: int,
        page: int,
        year: int | None = None,
    ) -> tuple[CatalogEntry, ...]:
        path = "/s/all" if page == 1 else f"/s/all/{page}"
        params: dict[str, int] = {"type": media_type, "order": order}
        if year is not None:
            params["year"] = year
        async with self._request_lock:
            loop = asyncio.get_running_loop()
            remaining = (
                self.request_interval_seconds
                - (loop.time() - self._last_request_at)
            )
            if remaining > 0:
                await asyncio.sleep(remaining)
            for attempt in range(6):
                try:
                    response = await self.client.get(
                        f"{self.origin}{path}",
                        params=params,
                        headers={"Referer": f"{self.origin}/"},
                    )
                except httpx.TransportError:
                    if attempt == 5:
                        raise
                    await asyncio.sleep(min(4.0 * (attempt + 1), 20.0))
                    continue
                self._last_request_at = loop.time()
                if response.status_code != 429 or attempt == 5:
                    break
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after else 4.0 * (attempt + 1)
                await asyncio.sleep(min(delay, 20.0))
            response.raise_for_status()
        return parse_catalog_page(response.text, self.origin)


def _entry_from_card(
    card: dict[str, str],
    origin: str,
) -> CatalogEntry | None:
    href = card.get("href", "")
    parsed = urlparse(urljoin(origin, href))
    match = _DETAIL_PATH_PATTERN.fullmatch(parsed.path)
    title = card.get("title", "").strip()
    if match is None or not title:
        return None
    description = card.get("description", "")
    year_match = _YEAR_PATTERN.match(description)
    release_year = int(year_match.group(1)) if year_match else None
    rating_match = _RATING_PATTERN.search(card.get("douban_rating", ""))
    available_episode_count, declared_episode_count = _episode_counts(
        description
    )
    return CatalogEntry(
        xlys_id=int(match.group(1)),
        title=title,
        year=release_year,
        cover_url=card.get("cover_url", "").strip() or None,
        source_updated_on=card.get("updated_at", "").strip(),
        dav_filename=movie_filename(title, release_year),
        source_url=parsed.geturl(),
        douban_rating=(float(rating_match.group()) if rating_match else None),
        available_episode_count=available_episode_count,
        declared_episode_count=declared_episode_count,
    )


def _episode_counts(description: str) -> tuple[int | None, int | None]:
    complete = _COMPLETE_EPISODES_PATTERN.search(description)
    if complete is not None:
        count = int(complete.group(1) or complete.group(2))
        return count, count
    updated = _UPDATED_EPISODES_PATTERN.search(description)
    if updated is not None:
        return int(updated.group(1)), None
    return None, None


def safe_media_name(title: str, year: int | None) -> str:
    safe_title = _INVALID_FILENAME_PATTERN.sub(" ", title)
    safe_title = re.sub(r"\s+", " ", safe_title).strip(" .") or "未命名影片"
    return safe_title + (f" ({year})" if year is not None else "")


def movie_filename(title: str, year: int | None) -> str:
    return safe_media_name(title, year) + ".strm"


def _deduplicate_filenames(
    entries: tuple[CatalogEntry, ...],
) -> tuple[CatalogEntry, ...]:
    counts: dict[str, int] = {}
    for entry in entries:
        key = entry.dav_filename.casefold()
        counts[key] = counts.get(key, 0) + 1
    return tuple(
        replace(
            entry,
            dav_filename=(
                entry.dav_filename[:-5]
                + f" [xlys-{entry.xlys_id}].strm"
                if counts[entry.dav_filename.casefold()] > 1
                else entry.dav_filename
            ),
        )
        for entry in entries
    )
