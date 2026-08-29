from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, cast

import httpx
from fastapi import Depends, FastAPI, Request

from .catalog import XlysCatalog
from .config import AppSettings, DavSettings
from .database import MovieRepository
from .library import MovieLibrary
from .xlys import XlysResolver


@dataclass(frozen=True, slots=True)
class AppServices:
    """Fully typed application services created for one FastAPI lifespan."""

    settings: AppSettings
    http: httpx.AsyncClient
    resolver: XlysResolver
    catalog: XlysCatalog
    movie_repository: MovieRepository
    movie_library: MovieLibrary


def set_app_services(application: FastAPI, services: AppServices) -> None:
    application.state.services = services


def get_app_services(application: FastAPI) -> AppServices:
    try:
        services = application.state.services
    except AttributeError as exc:
        raise RuntimeError("Application services are not initialized") from exc
    return cast(AppServices, services)


def get_services(request: Request) -> AppServices:
    return get_app_services(cast(FastAPI, request.app))


ServicesDep = Annotated[AppServices, Depends(get_services)]


def get_settings(services: ServicesDep) -> AppSettings:
    return services.settings


SettingsDep = Annotated[AppSettings, Depends(get_settings)]


def get_dav_settings(settings: SettingsDep) -> DavSettings:
    return settings.dav


DavSettingsDep = Annotated[DavSettings, Depends(get_dav_settings)]


def get_http_client(services: ServicesDep) -> httpx.AsyncClient:
    return services.http


HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]


def get_resolver(services: ServicesDep) -> XlysResolver:
    return services.resolver


ResolverDep = Annotated[XlysResolver, Depends(get_resolver)]


def get_catalog(services: ServicesDep) -> XlysCatalog:
    return services.catalog


CatalogDep = Annotated[XlysCatalog, Depends(get_catalog)]


def get_movie_repository(services: ServicesDep) -> MovieRepository:
    return services.movie_repository


MovieRepositoryDep = Annotated[MovieRepository, Depends(get_movie_repository)]


def get_movie_library(services: ServicesDep) -> MovieLibrary:
    return services.movie_library


MovieLibraryDep = Annotated[MovieLibrary, Depends(get_movie_library)]
