from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date, datetime
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .catalog import CatalogEntry, movie_filename
from .database import MediaItem, MediaRepository, MediaType, MoviePolicy
from .dependencies import (
    AdminAuthDep,
    HttpClientDep,
    MediaLibraryDep,
    MediaRepositoryDep,
    PlaybackCoordinatorDep,
    ResolverDep,
    SettingsDep,
)
from .detail import fetch_xlys_detail
from .library import MediaLibrary
from .logging_utils import describe_http_error
from .models import ResolvedPage, ResolverError
from .playback_selection import PlaybackCoordinator, PlaybackSelection


router = APIRouter(prefix="/api/admin", tags=["admin"])


class MovieItem(BaseModel):
    xlys_id: int
    title: str
    year: int | None
    cover_url: str | None
    douban_rating: float | None
    source_updated_on: date | None
    policy: MoviePolicy
    dav_filename: str
    play_page_url: str
    kind: Literal["movie", "series"]
    available_episode_count: int


class EpisodeItem(BaseModel):
    source_index: int
    label: str
    play_page_url: str


class MediaDetail(MovieItem):
    source_url: str | None
    declared_episode_count: int | None
    season_number: int | None
    last_checked_at: datetime | None
    last_watched_at: datetime | None
    episodes: list[EpisodeItem]


class MovieCounts(BaseModel):
    total: int
    automatic: int
    kept: int
    hidden: int
    movies: int
    series: int


class MovieCatalog(BaseModel):
    movies: list[MovieItem]
    counts: MovieCounts
    recent_limit: int
    recently_watched_series_count: int
    recently_watched_series: list[str]


class RefreshItem(BaseModel):
    xlys_id: int
    title: str
    kind: Literal["movie", "series"]
    added_episodes: int
    available_episode_count: int


class RefreshResult(BaseModel):
    item: RefreshItem
    catalog: MovieCatalog


class RefreshFailure(BaseModel):
    xlys_id: int
    title: str
    error: str


class RecentRefreshResult(BaseModel):
    checked: int
    refreshed: int
    added_episodes: int
    failures: list[RefreshFailure]
    catalog: MovieCatalog


class PolicyUpdate(BaseModel):
    policy: MoviePolicy


class MovieSelection(BaseModel):
    xlys_ids: list[int] = Field(min_length=1, max_length=500)


class BatchPolicyUpdate(MovieSelection):
    policy: MoviePolicy


class ManualImportRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class ManualImportResult(BaseModel):
    kind: Literal["movie", "series"]
    imported: bool
    xlys_id: int
    title: str
    year: int | None
    cover_url: str | None
    available_episode_count: int
    declared_episode_count: int | None
    source_url: str
    message: str
    catalog: MovieCatalog | None = None


class PlaybackRouteOption(BaseModel):
    line: int
    key: str
    name: str | None
    kind: str
    selectable: bool


class PlaybackRoutes(BaseModel):
    page_url: str
    title: str | None
    media_kind: Literal["movie", "series"]
    cache_enabled: bool
    cached_source: Literal["hls", "tos", "member", "url3"] | None
    manual_override: bool
    selected_line: int | None
    selected_route_name: str | None
    selected_route_key: str | None
    routes: list[PlaybackRouteOption]


class PlaybackRouteUpdate(BaseModel):
    route_name: str = Field(min_length=1, max_length=128)


class PlaybackRouteClearResult(BaseModel):
    cleared: Literal[True] = True
    page_url: str


class CredentialsView(BaseModel):
    username: str


class CredentialsUpdate(BaseModel):
    current_password: str
    username: str = Field(min_length=1, max_length=255, pattern=r"^[^:\s]+$")
    password: str = Field(min_length=8, max_length=1024)


@router.get("/credentials", response_model=CredentialsView)
def get_credentials(_auth: AdminAuthDep, repository: MediaRepositoryDep) -> CredentialsView:
    return CredentialsView(username=repository.credentials_username())


@router.put("/credentials", response_model=CredentialsView)
def update_credentials(
    update: CredentialsUpdate,
    _auth: AdminAuthDep,
    repository: MediaRepositoryDep,
) -> CredentialsView:
    if not repository.change_credentials(
        update.current_password, update.username, update.password
    ):
        raise HTTPException(status_code=403, detail="当前密码不正确")
    return CredentialsView(username=update.username)


@router.get("/movies", response_model=MovieCatalog, include_in_schema=False)
@router.get("/media", response_model=MovieCatalog)
async def list_movies(
    _auth: AdminAuthDep,
    repository: MediaRepositoryDep,
    library: MediaLibraryDep,
) -> MovieCatalog:
    await library.visible_movies()
    return _catalog(repository.list_media(), library)


@router.get("/media/{xlys_id}", response_model=MediaDetail)
async def get_media_detail(
    xlys_id: int,
    _auth: AdminAuthDep,
    repository: MediaRepositoryDep,
    library: MediaLibraryDep,
) -> MediaDetail:
    media = _require_media(repository, xlys_id)
    episodes = (
        repository.list_episodes(xlys_id)
        if media.media_type == MediaType.SERIES.value else ()
    )
    return MediaDetail(
        **_movie_item(media, library, len(episodes)).model_dump(),
        source_url=media.source_url,
        declared_episode_count=media.declared_episode_count,
        season_number=media.season_number,
        last_checked_at=media.last_checked_at,
        last_watched_at=media.last_watched_at,
        episodes=[
            EpisodeItem(
                source_index=episode.source_index,
                label=episode.label,
                play_page_url=library.episode_play_url(media, episode),
            )
            for episode in episodes
        ],
    )


@router.patch("/movies/batch/policy", response_model=MovieCatalog, include_in_schema=False)
@router.patch("/media/batch/policy", response_model=MovieCatalog)
async def update_movie_policies(
    update: BatchPolicyUpdate,
    _auth: AdminAuthDep,
    repository: MediaRepositoryDep,
    library: MediaLibraryDep,
) -> MovieCatalog:
    repository.set_media_policies(tuple(set(update.xlys_ids)), update.policy)
    return _catalog(repository.list_media(), library)


@router.delete("/movies/batch", response_model=MovieCatalog, include_in_schema=False)
@router.delete("/media/batch", response_model=MovieCatalog)
async def delete_movies(
    selection: MovieSelection,
    _auth: AdminAuthDep,
    repository: MediaRepositoryDep,
    library: MediaLibraryDep,
) -> MovieCatalog:
    repository.delete_media(tuple(set(selection.xlys_ids)))
    return _catalog(repository.list_media(), library)


@router.patch("/movies/{xlys_id}/policy", response_model=MovieItem, include_in_schema=False)
@router.patch("/media/{xlys_id}/policy", response_model=MovieItem)
async def update_movie_policy(
    xlys_id: int,
    update: PolicyUpdate,
    _auth: AdminAuthDep,
    repository: MediaRepositoryDep,
    library: MediaLibraryDep,
) -> MovieItem:
    if not repository.set_media_policy(xlys_id, update.policy):
        raise HTTPException(status_code=404, detail="Media not found")
    movie = repository.get_media(xlys_id)
    if movie is None:
        raise HTTPException(status_code=404, detail="Media not found")
    return _movie_item(
        movie,
        library,
        len(repository.list_episodes(xlys_id)) if movie.media_type == MediaType.SERIES.value else 0,
    )


@router.post("/sync", response_model=MovieCatalog)
async def sync_movies(
    _auth: AdminAuthDep,
    repository: MediaRepositoryDep,
    library: MediaLibraryDep,
) -> MovieCatalog:
    await library.sync_from_source()
    return _catalog(repository.list_media(), library)


@router.post("/media/refresh-recent-series", response_model=RecentRefreshResult)
async def refresh_recent_series(
    _auth: AdminAuthDep,
    http: HttpClientDep,
    settings: SettingsDep,
    repository: MediaRepositoryDep,
    library: MediaLibraryDep,
) -> RecentRefreshResult:
    targets = repository.recently_watched_series()
    semaphore = asyncio.Semaphore(3)

    async def refresh(media: MediaItem) -> RefreshItem | RefreshFailure:
        async with semaphore:
            try:
                return await _refresh_media(
                    media, http, settings.allowed_page_hosts, repository
                )
            except (HTTPException, httpx.HTTPError, ResolverError, ValueError) as exc:
                return RefreshFailure(
                    xlys_id=media.xlys_id,
                    title=media.title,
                    error=_refresh_error(exc),
                )

    results = await asyncio.gather(*(refresh(media) for media in targets))
    refreshed = [item for item in results if isinstance(item, RefreshItem)]
    return RecentRefreshResult(
        checked=len(targets),
        refreshed=len(refreshed),
        added_episodes=sum(item.added_episodes for item in refreshed),
        failures=[item for item in results if isinstance(item, RefreshFailure)],
        catalog=_catalog(repository.list_media(), library),
    )


@router.post("/media/{xlys_id}/refresh", response_model=RefreshResult)
async def refresh_media(
    xlys_id: int,
    _auth: AdminAuthDep,
    http: HttpClientDep,
    settings: SettingsDep,
    repository: MediaRepositoryDep,
    library: MediaLibraryDep,
) -> RefreshResult:
    media = _require_media(repository, xlys_id)
    item = await _refresh_media(media, http, settings.allowed_page_hosts, repository)
    return RefreshResult(item=item, catalog=_catalog(repository.list_media(), library))


@router.get(
    "/media/{xlys_id}/playback-routes",
    response_model=PlaybackRoutes,
)
async def get_playback_routes(
    xlys_id: int,
    _auth: AdminAuthDep,
    repository: MediaRepositoryDep,
    library: MediaLibraryDep,
    resolver: ResolverDep,
    playback: PlaybackCoordinatorDep,
) -> PlaybackRoutes:
    media = _require_media(repository, xlys_id)
    page_url = _media_play_page_url(media, repository, library)
    resolved = await resolver.resolve_page(page_url, refresh=True)
    return _playback_routes(media, resolved, playback)


@router.put(
    "/media/{xlys_id}/playback-routes",
    response_model=PlaybackRoutes,
)
async def set_playback_route(
    xlys_id: int,
    update: PlaybackRouteUpdate,
    _auth: AdminAuthDep,
    repository: MediaRepositoryDep,
    library: MediaLibraryDep,
    resolver: ResolverDep,
    playback: PlaybackCoordinatorDep,
) -> PlaybackRoutes:
    if not playback.cache.enabled:
        raise HTTPException(
            status_code=409,
            detail="Playback selection cache is disabled",
        )
    media = _require_media(repository, xlys_id)
    page_url = _media_play_page_url(media, repository, library)
    resolved = await resolver.resolve_page(page_url)
    key = update.route_name
    direct_line: int | None = None
    if key == "source:tos":
        if not resolved.tos_available:
            raise HTTPException(status_code=404, detail="TOS route is not available")
    elif key.startswith("url3:"):
        try:
            direct_line = int(key[5:])
        except ValueError:
            raise HTTPException(status_code=404, detail="Direct route is not available") from None
        if direct_line < 0 or direct_line >= len(resolved.direct_candidates):
            raise HTTPException(status_code=404, detail="Direct route is not available")
        key = f"url3:{direct_line}"
    else:
        wanted = key.casefold()
        matches = [
            index
            for index, candidate in enumerate(resolved.candidates)
            if candidate.route_name is not None
            and candidate.route_name.casefold() == wanted
        ]
        if not matches:
            raise HTTPException(
                status_code=404,
                detail=f"Playback route {key!r} is not available",
            )
        if len(matches) > 1:
            raise HTTPException(
                status_code=409,
                detail=f"Playback route {key!r} is ambiguous",
            )
        key = resolved.candidates[matches[0]].route_name
    if media.media_type == MediaType.SERIES.value:
        playback.cache.remember_series_route(
            media.xlys_id,
            key,
            _episode_play_page_urls(media, repository, library),
        )
    elif key == "source:tos":
        playback.cache.remember_manual_direct(resolved, "tos")
    elif direct_line is not None:
        playback.cache.remember_manual_direct(resolved, "url3", direct_line)
    else:
        playback.cache.remember_hls(
            resolved,
            matches[0],
            manual_override=True,
        )
    return _playback_routes(media, resolved, playback)


@router.delete(
    "/media/{xlys_id}/playback-routes",
    response_model=PlaybackRouteClearResult,
)
async def clear_playback_route(
    xlys_id: int,
    _auth: AdminAuthDep,
    repository: MediaRepositoryDep,
    library: MediaLibraryDep,
    playback: PlaybackCoordinatorDep,
) -> PlaybackRouteClearResult:
    if not playback.cache.enabled:
        raise HTTPException(
            status_code=409,
            detail="Playback selection cache is disabled",
        )
    media = _require_media(repository, xlys_id)
    page_url = _media_play_page_url(media, repository, library)
    if media.media_type == MediaType.SERIES.value:
        playback.cache.clear_series_route(
            media.xlys_id, _episode_play_page_urls(media, repository, library)
        )
    else:
        playback.cache.clear(page_url)
    return PlaybackRouteClearResult(page_url=page_url)


@router.post("/import", response_model=ManualImportResult)
async def import_from_url(
    request: ManualImportRequest,
    _auth: AdminAuthDep,
    http: HttpClientDep,
    settings: SettingsDep,
    repository: MediaRepositoryDep,
    library: MediaLibraryDep,
) -> ManualImportResult:
    detail = await fetch_xlys_detail(
        http,
        request.url,
        allowed_hosts=settings.allowed_page_hosts,
    )
    common = {
        "kind": detail.kind,
        "xlys_id": detail.xlys_id,
        "title": detail.title,
        "year": detail.year,
        "cover_url": detail.cover_url,
        "available_episode_count": len(detail.episodes),
        "declared_episode_count": detail.declared_episode_count,
        "source_url": detail.source_url,
    }
    if detail.kind == "series":
        repository.import_discovered_series(
            (
                CatalogEntry(
                    xlys_id=detail.xlys_id,
                    title=detail.title,
                    year=detail.year,
                    cover_url=detail.cover_url,
                    source_updated_on=detail.source_updated_on or "",
                    dav_filename=movie_filename(detail.title, detail.year),
                    source_url=detail.source_url,
                ),
            ),
            (detail,),
        )
        repository.set_series_policy(detail.xlys_id, MoviePolicy.KEEP)
        return ManualImportResult(
            **common,
            imported=True,
            message="电视剧及当前分集已添加，并设为人工保留。",
            catalog=_catalog(repository.list_media(), library),
        )

    repository.import_discovered_movies(
        (
            CatalogEntry(
                xlys_id=detail.xlys_id,
                title=detail.title,
                year=detail.year,
                cover_url=detail.cover_url,
                source_updated_on=detail.source_updated_on or "",
                dav_filename=movie_filename(detail.title, detail.year),
                source_url=detail.source_url,
            ),
        )
    )
    repository.set_movie_policy(detail.xlys_id, MoviePolicy.KEEP)
    return ManualImportResult(
        **common,
        imported=True,
        message="影片已添加，并设为人工保留。",
        catalog=_catalog(repository.list_media(), library),
    )


def _catalog(movies: tuple[MediaItem, ...], library: MediaLibrary) -> MovieCatalog:
    episode_counts = library.repository.episode_counts()
    recently_watched_series = library.repository.recently_watched_series()
    counts = MovieCounts(
        total=len(movies),
        automatic=sum(movie.policy == MoviePolicy.AUTO.value for movie in movies),
        kept=sum(movie.policy == MoviePolicy.KEEP.value for movie in movies),
        hidden=sum(movie.policy == MoviePolicy.HIDDEN.value for movie in movies),
        movies=sum(movie.media_type == MediaType.MOVIE.value for movie in movies),
        series=sum(movie.media_type == MediaType.SERIES.value for movie in movies),
    )
    return MovieCatalog(
        movies=[_movie_item(movie, library, episode_counts.get(movie.xlys_id, 0)) for movie in movies],
        counts=counts,
        recent_limit=library.recent_limit,
        recently_watched_series_count=len(recently_watched_series),
        recently_watched_series=[series.title for series in recently_watched_series],
    )


async def _refresh_media(
    media: MediaItem,
    http: httpx.AsyncClient,
    allowed_hosts: tuple[str, ...],
    repository: MediaRepository,
) -> RefreshItem:
    if not media.source_url:
        raise HTTPException(status_code=409, detail="该影片没有详情页地址，无法刷新")
    detail = await fetch_xlys_detail(
        http, media.source_url, allowed_hosts=allowed_hosts
    )
    if detail.xlys_id != media.xlys_id or detail.kind != media.media_type:
        raise HTTPException(status_code=409, detail="详情页与现有影片不匹配")
    added = repository.refresh_media_from_detail(detail)
    return RefreshItem(
        xlys_id=media.xlys_id,
        title=detail.title,
        kind=detail.kind,
        added_episodes=added,
        available_episode_count=(
            len(repository.list_episodes(media.xlys_id))
            if detail.kind == MediaType.SERIES.value else 0
        ),
    )


def _refresh_error(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, httpx.HTTPError):
        return describe_http_error(exc)
    return str(exc)


def _require_media(repository: MediaRepository, xlys_id: int) -> MediaItem:
    media = repository.get_media(xlys_id)
    if media is None:
        raise HTTPException(status_code=404, detail="Media not found")
    return media


def _media_play_page_url(
    media: MediaItem,
    repository: MediaRepository,
    library: MediaLibrary,
) -> str:
    if media.media_type == MediaType.SERIES.value:
        episodes = repository.list_episodes(media.xlys_id)
        if episodes:
            return library.episode_play_url(media, episodes[0])
    return library.movie_play_url(media)


def _episode_play_page_urls(
    media: MediaItem,
    repository: MediaRepository,
    library: MediaLibrary,
) -> tuple[str, ...]:
    return tuple(
        library.episode_play_url(media, episode)
        for episode in repository.list_episodes(media.xlys_id)
    )


def _playback_routes(
    media: MediaItem,
    resolved: ResolvedPage,
    playback: PlaybackCoordinator,
) -> PlaybackRoutes:
    selection = playback.cache.get(resolved)
    return PlaybackRoutes(
        page_url=resolved.page_url,
        title=resolved.title,
        media_kind=media.media_type,
        cache_enabled=playback.cache.enabled,
        cached_source=selection.source if selection is not None else None,
        manual_override=(
            selection.manual_override if selection is not None else False
        ),
        selected_line=(
            selection.line
            if selection is not None and selection.source == "hls"
            else None
        ),
        selected_route_name=_selected_route_name(selection),
        selected_route_key=_selected_route_key(selection),
        routes=_route_options(resolved),
    )


def _route_options(resolved: ResolvedPage) -> list[PlaybackRouteOption]:
    counts = Counter(
        candidate.route_name.casefold()
        for candidate in resolved.candidates
        if candidate.route_name is not None
    )
    routes = [
        PlaybackRouteOption(
            line=index,
            key=candidate.route_name or f"hls:{index}",
            name=candidate.route_name,
            kind=candidate.kind,
            selectable=(
                candidate.route_name is not None
                and counts[candidate.route_name.casefold()] == 1
            ),
        )
        for index, candidate in enumerate(resolved.candidates)
    ]
    for index, _candidate in enumerate(resolved.direct_candidates):
        routes.append(PlaybackRouteOption(
            line=len(routes), key=f"url3:{index}",
            name=f"直连线路 {index + 1}", kind="url3", selectable=True,
        ))
    if resolved.tos_available:
        routes.append(PlaybackRouteOption(
            line=len(routes), key="source:tos",
            name="TOS 直连", kind="tos", selectable=True,
        ))
    if resolved.member_token is not None:
        routes.append(PlaybackRouteOption(
            line=len(routes), key="source:member",
            name="会员线路", kind="member", selectable=False,
        ))
    return routes


def _selected_route_key(selection: PlaybackSelection | None) -> str | None:
    if selection is None:
        return None
    if selection.source == "hls":
        return selection.route_name
    if selection.manual_override and selection.source == "tos":
        return "source:tos"
    if selection.source == "url3" and selection.line is not None:
        return f"url3:{selection.line}"
    return None


def _selected_route_name(selection: PlaybackSelection | None) -> str | None:
    if selection is None:
        return None
    if selection.source == "hls":
        return selection.route_name
    if selection.manual_override and selection.source == "tos":
        return "TOS 直连"
    if selection.source == "url3" and selection.line is not None:
        return f"直连线路 {selection.line + 1}"
    return None


def _movie_item(
    movie: MediaItem,
    library: MediaLibrary,
    available_episode_count: int,
) -> MovieItem:
    return MovieItem(
        xlys_id=movie.xlys_id,
        title=movie.title,
        year=movie.year,
        cover_url=movie.cover_url,
        douban_rating=movie.douban_rating,
        source_updated_on=movie.source_updated_on,
        policy=MoviePolicy(movie.policy),
        dav_filename=movie.dav_name,
        play_page_url=library.movie_play_url(movie),
        kind=movie.media_type,
        available_episode_count=available_episode_count,
    )
