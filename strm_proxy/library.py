from __future__ import annotations

import asyncio

from .catalog import XlysCatalog
from .database import Movie, MovieRepository


class MovieLibrary:
    """Persist discovery results and expose the database-backed DAV view."""

    def __init__(
        self,
        repository: MovieRepository,
        catalog: XlysCatalog,
        *,
        auto_limit: int,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.auto_limit = auto_limit
        self._bootstrap_lock = asyncio.Lock()

    async def visible_movies(self) -> tuple[Movie, ...]:
        await self._bootstrap_if_empty()
        return self.repository.list_visible(self.auto_limit)

    async def find_visible_by_filename(self, filename: str) -> Movie | None:
        return next(
            (
                movie
                for movie in await self.visible_movies()
                if movie.dav_filename == filename
            ),
            None,
        )

    async def sync_from_source(self) -> tuple[Movie, ...]:
        discovered = await self.catalog.latest_movies()
        self.repository.import_discovered(discovered)
        return self.repository.list_visible(self.auto_limit)

    def play_url(self, movie: Movie) -> str:
        return f"{self.catalog.origin}/play/{movie.xlys_id}-0.htm"

    async def _bootstrap_if_empty(self) -> None:
        if self.repository.has_movies():
            return
        async with self._bootstrap_lock:
            if not self.repository.has_movies():
                await self.sync_from_source()
