from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import date
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx

from .cache import TTLCache
from .libvio import (
    DETAIL_PATH,
    LIBVIO_HOSTS,
    LibvioDetail,
    LibvioNoOnlineRoutes,
    parse_libvio_detail,
)
from .models import ResolverError

logger = logging.getLogger(__name__)
Kind = Literal["movie", "series"]


@dataclass(frozen=True, slots=True)
class LibvioCard:
    source_media_id: str
    title: str
    source_url: str
    rating: float | None


@dataclass(frozen=True, slots=True)
class LibvioDiscovery:
    details: tuple[LibvioDetail, ...]
    failures: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.failures


class _ListParser(HTMLParser):
    def __init__(self, origin: str) -> None:
        super().__init__(convert_charrefs=True)
        self.origin = origin
        self.cards: list[LibvioCard] = []
        self.has_listing = False
        self.next_url: str | None = None
        self._list_depth = 0
        self._card: LibvioCard | None = None
        self._rating: list[str] | None = None
        self._anchor: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "ul":
            if self._list_depth:
                self._list_depth += 1
            elif "stui-vodlist" in classes:
                self.has_listing = True
                self._list_depth = 1
        if tag == "a":
            href = values.get("href") or ""
            self._anchor = (href, [])
            if self._list_depth and "stui-vodlist__thumb" in classes:
                parsed = urlsplit(urljoin(self.origin, href))
                match = DETAIL_PATH.fullmatch(parsed.path)
                if (
                    match
                    and parsed.scheme == "https"
                    and parsed.hostname in LIBVIO_HOSTS
                    and not parsed.query
                    and not parsed.fragment
                    and not parsed.username
                    and parsed.port in {None, 443}
                ):
                    self._card = LibvioCard(
                        match[1],
                        values.get("title") or "",
                        f"https://{parsed.hostname}{parsed.path}",
                        None,
                    )
        elif tag == "span" and "pic-tag" in classes and self._card:
            self._rating = []

    def handle_data(self, data: str) -> None:
        if self._rating is not None:
            self._rating.append(data)
        if self._anchor:
            self._anchor[1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "ul" and self._list_depth:
            self._list_depth -= 1
        if tag == "span" and self._rating is not None:
            try:
                value = float("".join(self._rating).strip())
                if 0 <= value <= 10 and self._card:
                    self._card = replace(self._card, rating=value)
            except ValueError:
                pass
            self._rating = None
        if tag == "a":
            if self._card:
                self.cards.append(self._card)
                self._card = None
            if self._anchor and "".join(self._anchor[1]).strip() in {"下一页", "下页"}:
                self.next_url = urljoin(self.origin, self._anchor[0])
            self._anchor = None


def parse_libvio_list(html: str, origin: str = "https://www.libvio.lat") -> _ListParser:
    parser = _ListParser(origin)
    parser.feed(html)
    parser.close()
    if not parser.has_listing:
        raise ResolverError("LIBVIO 分类页没有影片列表，保留上次的自动片库")
    return parser


class LibvioCatalog:
    """Discover recent and highly rated online titles without requesting videos."""

    origin = "https://www.libvio.lat"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        recent_limit: int = 50,
        year_span: int = 4,
        movie_rating_threshold: float = 7.0,
        series_rating_threshold: float = 8.0,
        cache_ttl_seconds: float = 1800,
        current_year: int | None = None,
        request_interval_seconds: float = 0.25,
        detail_concurrency: int = 3,
        max_pages: int = 200,
    ) -> None:
        self.client = client
        self.recent_limit = recent_limit
        self.year_span = year_span
        self.movie_rating_threshold = movie_rating_threshold
        self.series_rating_threshold = series_rating_threshold
        self.current_year = current_year
        self._interval = request_interval_seconds
        self._detail_slots = asyncio.Semaphore(detail_concurrency)
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._max_pages = max_pages
        self._cache: TTLCache[str, LibvioDiscovery] = TTLCache(cache_ttl_seconds)
        self._locks = {"movie": asyncio.Lock(), "series": asyncio.Lock()}

    async def discover_movies(self) -> LibvioDiscovery:
        return await self._discover("movie")

    async def discover_series(self) -> LibvioDiscovery:
        return await self._discover("series")

    async def _discover(self, kind: Kind) -> LibvioDiscovery:
        cached = self._cache.get(kind)
        if cached is not None:
            return cached
        async with self._locks[kind]:
            cached = self._cache.get(kind)
            if cached is not None:
                return cached
            year = self.current_year or date.today().year
            threshold = (
                self.series_rating_threshold if kind == "series" else self.movie_rating_threshold
            )
            groups = await asyncio.gather(
                self._list(kind, order="time"),
                *(
                    self._list(kind, order="score", year=value, threshold=threshold)
                    for value in range(year, year - self.year_span, -1)
                ),
                return_exceptions=True,
            )
            successful_groups: list[tuple[LibvioCard, ...]] = []
            for group in groups:
                if isinstance(group, BaseException):
                    raise group
                successful_groups.append(group)
            # Recent IDs win if a title also appears in a rated-year list.
            cards = {
                card.source_media_id: card
                for group in reversed(successful_groups)
                for card in group
            }
            if not cards:
                raise ResolverError("LIBVIO 未发现任何影片，保留上次的自动片库")
            failures: list[str] = []

            async def fetch(card: LibvioCard) -> LibvioDetail | None:
                async with self._detail_slots:
                    try:
                        detail = parse_libvio_detail(
                            await self._get(card.source_url),
                            source_url=card.source_url,
                            kind_hint=kind,
                        )
                        return detail
                    except LibvioNoOnlineRoutes:
                        logger.info(
                            "event=libvio_discovery_download_only source_id=%s",
                            card.source_media_id,
                        )
                        return None
                    except (httpx.HTTPError, ResolverError, ValueError) as exc:
                        # Any incomplete detail snapshot disables pruning for this
                        # kind. Successful details may still refresh or add titles.
                        failures.append(
                            f"LIBVIO {card.title}（{card.source_media_id}）详情获取失败"
                        )
                        logger.warning(
                            "event=libvio_discovery_detail_failed source_id=%s reason=%s",
                            card.source_media_id,
                            str(exc) if isinstance(exc, ResolverError) else type(exc).__name__,
                        )
                        return None

            details = tuple(
                item
                for item in await asyncio.gather(*(fetch(card) for card in cards.values()))
                if item is not None
            )
            result = LibvioDiscovery(details, tuple(failures))
            if result.complete:
                self._cache.set(kind, result)
            logger.info(
                "event=libvio_discovery_complete kind=%s candidates=%d online=%d failures=%d",
                kind,
                len(cards),
                len(details),
                len(failures),
            )
            return result

    @staticmethod
    def _path(kind: Kind, order: str, page: int, year: int | None) -> str:
        parts = [""] * 12
        parts[0] = "2" if kind == "series" else "1"
        parts[2] = order
        parts[8] = str(page) if page > 1 else ""
        parts[11] = str(year) if year is not None else ""
        return "/show/" + "-".join(parts) + ".html"

    async def _list(
        self, kind: Kind, *, order: str, year: int | None = None, threshold: float = 0
    ) -> tuple[LibvioCard, ...]:
        entries: dict[str, LibvioCard] = {}
        for page in range(1, self._max_pages + 1):
            path = self._path(kind, order, page, year)
            listing = parse_libvio_list(await self._get(self.origin + path), self.origin)
            if not listing.cards:
                return tuple(entries.values())
            new_ids = {card.source_media_id for card in listing.cards} - entries.keys()
            if not new_ids:
                raise ResolverError("LIBVIO 翻页重复，保留上次的自动片库")
            for card in listing.cards:
                if order == "score" and (card.rating is None or card.rating <= threshold):
                    return tuple(entries.values())
                entries.setdefault(card.source_media_id, card)
                if order == "time" and len(entries) >= self.recent_limit:
                    return tuple(entries.values())
            if listing.next_url is None:
                return tuple(entries.values())
            expected = self.origin + self._path(kind, order, page + 1, year)
            if listing.next_url != expected:
                raise ResolverError("LIBVIO 分页链接不符合当前筛选条件")
        raise ResolverError("LIBVIO 翻页超出限制，保留上次的自动片库")

    async def _get(self, url: str) -> str:
        for attempt in range(3):
            async with self._request_lock:
                loop = asyncio.get_running_loop()
                delay = self._interval - (loop.time() - self._last_request_at)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_request_at = loop.time()
            try:
                response = await self.client.get(url)
                response.raise_for_status()
                return response.text
            except httpx.HTTPStatusError as exc:
                if attempt == 2 or (
                    exc.response.status_code != 429 and exc.response.status_code < 500
                ):
                    raise
            except httpx.TransportError:
                if attempt == 2:
                    raise
            await asyncio.sleep(1 + attempt)
        raise AssertionError("unreachable")
