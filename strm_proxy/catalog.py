from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from .cache import TTLCache
from .models import ResolverError


_DETAIL_PATH_PATTERN = re.compile(r"^/[^/?#]+/(\d+)\.htm$")
_YEAR_PATTERN = re.compile(r"^\s*((?:19|20)\d{2})")
_INVALID_FILENAME_PATTERN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass(frozen=True, slots=True)
class CatalogMovie:
    xlys_id: int
    title: str
    year: int | None
    cover_url: str | None
    source_updated_on: str
    dav_filename: str


class _MovieCardParser(HTMLParser):
    def __init__(self, origin: str) -> None:
        super().__init__(convert_charrefs=True)
        self.origin = origin
        self.movies: list[CatalogMovie] = []
        self._card_depth = 0
        self._card: dict[str, str] | None = None
        self._capture: str | None = None
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
        if tag == "a" and "card-img" in classes:
            self._card["href"] = values.get("href") or ""
            self._card["description"] = values.get("title") or ""
        elif tag == "img" and "cover_url" not in self._card:
            self._card["cover_url"] = (
                values.get("data-src") or values.get("src") or ""
            )
        elif tag == "h4":
            self._start_capture("title")
        elif tag == "span" and self._meta_depth:
            self._start_capture("updated_at")

    def handle_endtag(self, tag: str) -> None:
        if self._card is None:
            return
        if self._capture and self._capture_depth == self._card_depth:
            self._card[self._capture] = "".join(self._text).strip()
            self._capture = None
            self._text = []
        if tag == "div":
            if self._meta_depth == self._card_depth:
                self._meta_depth = 0
            self._card_depth -= 1
            if self._card_depth == 0:
                movie = _movie_from_card(self._card, self.origin)
                if movie is not None:
                    self.movies.append(movie)
                self._card = None

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._text.append(data)

    def _start_capture(self, name: str) -> None:
        self._capture = name
        self._capture_depth = self._card_depth
        self._text = []


def parse_catalog_page(html: str, origin: str) -> tuple[CatalogMovie, ...]:
    parser = _MovieCardParser(origin)
    parser.feed(html)
    parser.close()
    return tuple(parser.movies)


class XlysCatalog:
    """Discover the newest movies while keeping WebDAV scans inexpensive."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        origin: str = "https://www.xlys02.com",
        limit: int = 100,
        cache_ttl_seconds: float = 1800,
    ) -> None:
        self.client = client
        self.origin = origin.rstrip("/")
        self.limit = limit
        self._cache = TTLCache[str, tuple[CatalogMovie, ...]](cache_ttl_seconds)
        self._last_good: tuple[CatalogMovie, ...] | None = None
        self._refresh_lock = asyncio.Lock()

    async def latest_movies(self) -> tuple[CatalogMovie, ...]:
        if cached := self._cache.get("latest"):
            return cached
        async with self._refresh_lock:
            if cached := self._cache.get("latest"):
                return cached
            try:
                movies = await self._fetch_latest()
            except (httpx.HTTPError, ResolverError):
                if self._last_good is not None:
                    return self._last_good
                raise
            self._cache.set("latest", movies)
            self._last_good = movies
            return movies

    async def _fetch_latest(self) -> tuple[CatalogMovie, ...]:
        movies: list[CatalogMovie] = []
        seen_ids: set[int] = set()
        page = 1
        while len(movies) < self.limit:
            path = "/s/all" if page == 1 else f"/s/all/{page}"
            response = await self.client.get(
                f"{self.origin}{path}",
                params={"type": 0, "order": 0},
                headers={"Referer": f"{self.origin}/"},
            )
            response.raise_for_status()
            page_movies = parse_catalog_page(response.text, self.origin)
            if not page_movies:
                break
            previous_count = len(movies)
            for movie in page_movies:
                if movie.xlys_id not in seen_ids:
                    movies.append(movie)
                    seen_ids.add(movie.xlys_id)
                    if len(movies) == self.limit:
                        break
            if len(movies) == previous_count:
                break
            page += 1

        if not movies:
            raise ResolverError("The xlys movie catalog did not contain any movie cards")
        return _deduplicate_filenames(tuple(movies))


def _movie_from_card(card: dict[str, str], origin: str) -> CatalogMovie | None:
    href = card.get("href", "")
    parsed = urlparse(urljoin(origin, href))
    match = _DETAIL_PATH_PATTERN.fullmatch(parsed.path)
    title = card.get("title", "").strip()
    if match is None or not title:
        return None
    xlys_id = int(match.group(1))
    description = card.get("description", "")
    year_match = _YEAR_PATTERN.match(description)
    release_year = int(year_match.group(1)) if year_match else None
    filename = _movie_filename(title, release_year)
    return CatalogMovie(
        xlys_id=xlys_id,
        title=title,
        year=release_year,
        cover_url=card.get("cover_url", "").strip() or None,
        source_updated_on=card.get("updated_at", "").strip(),
        dav_filename=filename,
    )


def _movie_filename(title: str, year: int | None) -> str:
    safe_title = _INVALID_FILENAME_PATTERN.sub(" ", title)
    safe_title = re.sub(r"\s+", " ", safe_title).strip(" .") or "未命名影片"
    suffix = f" ({year})" if year is not None else ""
    return f"{safe_title}{suffix}.strm"


def _deduplicate_filenames(
    movies: tuple[CatalogMovie, ...],
) -> tuple[CatalogMovie, ...]:
    counts: dict[str, int] = {}
    for movie in movies:
        key = movie.dav_filename.casefold()
        counts[key] = counts.get(key, 0) + 1
    return tuple(
        replace(
            movie,
            dav_filename=(
                movie.dav_filename[:-5] + f" [xlys-{movie.xlys_id}].strm"
                if counts[movie.dav_filename.casefold()] > 1
                else movie.dav_filename
            ),
        )
        for movie in movies
    )
