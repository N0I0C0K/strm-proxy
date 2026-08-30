from __future__ import annotations

from dataclasses import dataclass
import secrets
from typing import Annotated, cast

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .catalog import XlysCatalog
from .config import AppSettings, DavSettings
from .database import MediaRepository
from .library import MediaLibrary
from .xlys import XlysResolver


@dataclass(frozen=True, slots=True)
class AppServices:
    """Fully typed application services created for one FastAPI lifespan."""

    settings: AppSettings
    http: httpx.AsyncClient
    resolver: XlysResolver
    catalog: XlysCatalog
    media_repository: MediaRepository
    media_library: MediaLibrary


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


def get_media_repository(services: ServicesDep) -> MediaRepository:
    return services.media_repository


MediaRepositoryDep = Annotated[MediaRepository, Depends(get_media_repository)]


def get_media_library(services: ServicesDep) -> MediaLibrary:
    return services.media_library


MediaLibraryDep = Annotated[MediaLibrary, Depends(get_media_library)]


_admin_basic_auth = HTTPBasic(auto_error=False)


def require_admin_auth(
    settings: DavSettingsDep,
    credentials: Annotated[
        HTTPBasicCredentials | None,
        Depends(_admin_basic_auth),
    ],
) -> None:
    if credentials is not None and secrets.compare_digest(
        credentials.username,
        settings.username,
    ) and secrets.compare_digest(credentials.password, settings.password):
        return
    raise HTTPException(
        status_code=401,
        detail="Management authentication required",
        headers={"WWW-Authenticate": 'Basic realm="strm-proxy admin"'},
    )


AdminAuthDep = Annotated[None, Depends(require_admin_auth)]
