from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .catalog import CatalogEntry, movie_filename
from .database import MediaItem, MediaType, MoviePolicy
from .dependencies import (
    AdminAuthDep,
    HttpClientDep,
    MediaLibraryDep,
    MediaRepositoryDep,
    SettingsDep,
)
from .detail import fetch_xlys_detail
from .library import MediaLibrary


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


@router.get("/movies", response_model=MovieCatalog, include_in_schema=False)
@router.get("/media", response_model=MovieCatalog)
async def list_movies(
    _auth: AdminAuthDep,
    repository: MediaRepositoryDep,
    library: MediaLibraryDep,
) -> MovieCatalog:
    await library.visible_movies()
    return _catalog(repository.list_media(), library)


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
    return _movie_item(movie, library)


@router.post("/sync", response_model=MovieCatalog)
async def sync_movies(
    _auth: AdminAuthDep,
    repository: MediaRepositoryDep,
    library: MediaLibraryDep,
) -> MovieCatalog:
    await library.sync_from_source()
    return _catalog(repository.list_media(), library)


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
    counts = MovieCounts(
        total=len(movies),
        automatic=sum(movie.policy == MoviePolicy.AUTO.value for movie in movies),
        kept=sum(movie.policy == MoviePolicy.KEEP.value for movie in movies),
        hidden=sum(movie.policy == MoviePolicy.HIDDEN.value for movie in movies),
        movies=sum(movie.media_type == MediaType.MOVIE.value for movie in movies),
        series=sum(movie.media_type == MediaType.SERIES.value for movie in movies),
    )
    return MovieCatalog(
        movies=[_movie_item(movie, library) for movie in movies],
        counts=counts,
        recent_limit=library.recent_limit,
    )


def _movie_item(movie: MediaItem, library: MediaLibrary) -> MovieItem:
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
    )
