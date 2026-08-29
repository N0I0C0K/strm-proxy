from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import AppSettings
from .catalog import XlysCatalog
from .database import create_movie_repository
from .dependencies import AppServices, set_app_services
from .library import MovieLibrary
from .models import ResolverError
from .routes import router as api_router
from .webdav import router as webdav_router
from .xlys import XlysResolver


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or AppSettings.from_environment()
    settings.validate()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        repository = create_movie_repository(settings.database_path)
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(
                settings.request_timeout_seconds,
                connect=settings.connect_timeout_seconds,
            ),
            transport=httpx.AsyncHTTPTransport(retries=2),
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        ) as client:
            resolver = XlysResolver(
                client,
                allowed_hosts=settings.allowed_page_hosts,
                cache_ttl_seconds=settings.cache_ttl_seconds,
            )
            catalog = XlysCatalog(
                client,
                limit=settings.catalog_limit,
                cache_ttl_seconds=settings.catalog_cache_ttl_seconds,
            )
            movie_library = MovieLibrary(
                repository,
                catalog,
                auto_limit=settings.catalog_limit,
            )
            set_app_services(
                application,
                AppServices(
                    settings=settings,
                    http=client,
                    resolver=resolver,
                    catalog=catalog,
                    movie_repository=repository,
                    movie_library=movie_library,
                ),
            )
            try:
                yield
            finally:
                repository.close()

    application = FastAPI(
        title="STRM HLS Proxy Demo",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.include_router(api_router)
    application.include_router(webdav_router)

    @application.exception_handler(ResolverError)
    async def resolver_error_handler(
        _request: Request,
        exc: ResolverError,
    ) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @application.exception_handler(httpx.HTTPError)
    async def upstream_error_handler(
        _request: Request,
        exc: httpx.HTTPError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={"detail": f"Upstream request failed: {exc}"},
        )

    return application


default_settings = AppSettings.from_environment()
app = create_app(default_settings)


def run() -> None:
    uvicorn.run(
        app,
        host=default_settings.host,
        port=default_settings.port,
        reload=False,
    )
