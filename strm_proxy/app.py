from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .admin import router as admin_router
from .config import AppSettings
from .catalog import XlysCatalog
from .database import CacheRepository, create_media_repository
from .dependencies import AppServices, set_app_services
from .library import MediaLibrary
from .logging_utils import configure_logging, describe_http_error, safe_url_for_log
from .models import ResolverError
from .playback_selection import PlaybackCoordinator, PlaybackSelectionCache
from .routes import router as api_router
from .segment_cache import SegmentCache
from .webdav import router as webdav_router
from .xlys import XlysResolver


logger = logging.getLogger(__name__)


def build_xlys_cookie_jar(settings: AppSettings) -> httpx.Cookies:
    """Build domain-scoped login cookies that cannot reach media CDNs."""
    cookies = httpx.Cookies()
    if not settings.has_xlys_login:
        return cookies

    cookie_domains = {
        host
        for host in settings.allowed_page_hosts
        if not any(
            host != other and host.endswith(f".{other}")
            for other in settings.allowed_page_hosts
        )
    }
    for domain in cookie_domains:
        cookies.set("username", settings.xlys_username or "", domain=domain, path="/")
        cookies.set("password", settings.xlys_password or "", domain=domain, path="/")
    return cookies


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or AppSettings.from_environment()
    settings.validate()
    configure_logging(settings.log_level)
    prefer_raw_segments = not settings.proxy_segments
    logger.info(
        "event=app_config host=%s port=%d log_level=%s proxy_segments=%s "
        "play_selection_cache=%s hls_selection=%s "
        "segment_cache_enabled=%s segment_prefetch_seconds=%s "
        "segment_cache_max_mb=%d "
        "xlys_login=%s request_timeout_seconds=%s "
        "connect_timeout_seconds=%s cache_ttl_seconds=%s database=%s",
        settings.host,
        settings.port,
        settings.log_level,
        settings.proxy_segments,
        settings.play_selection_cache,
        "raw_first" if prefer_raw_segments else "first_healthy",
        settings.proxy_segments and settings.segment_cache_max_mb > 0,
        settings.segment_prefetch_seconds,
        settings.segment_cache_max_mb,
        settings.has_xlys_login,
        settings.request_timeout_seconds,
        settings.connect_timeout_seconds,
        settings.cache_ttl_seconds,
        settings.database_path,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        logger.info("event=app_start")
        repository = create_media_repository(settings.database_path)
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
            cookies=build_xlys_cookie_jar(settings),
        ) as client:
            resolver = XlysResolver(
                client,
                allowed_hosts=settings.allowed_page_hosts,
                cache_ttl_seconds=settings.cache_ttl_seconds,
                member_access_enabled=settings.has_xlys_login,
                segment_hosts=(settings.segment_host,),
                prefer_raw_segments=prefer_raw_segments,
            )
            catalog = XlysCatalog(
                client,
                recent_limit=settings.discovery_recent_limit,
                year_span=settings.discovery_year_span,
                movie_rating_threshold=settings.douban_rating_threshold,
                series_rating_threshold=(
                    settings.series_douban_rating_threshold
                ),
                cache_ttl_seconds=settings.catalog_cache_ttl_seconds,
            )
            media_library = MediaLibrary(
                repository,
                catalog,
            )
            playback = PlaybackCoordinator(
                PlaybackSelectionCache(
                    CacheRepository(repository.engine),
                    enabled=settings.play_selection_cache,
                )
            )
            segment_cache = SegmentCache(
                client,
                segment_host=settings.segment_host,
                max_bytes=(
                    settings.segment_cache_max_mb * 1024 * 1024
                    if settings.proxy_segments
                    else 0
                ),
                prefetch_seconds=settings.segment_prefetch_seconds,
            )
            set_app_services(
                application,
                AppServices(
                    settings=settings,
                    http=client,
                    resolver=resolver,
                    playback=playback,
                    segment_cache=segment_cache,
                    catalog=catalog,
                    media_repository=repository,
                    media_library=media_library,
                ),
            )
            try:
                yield
            finally:
                await segment_cache.aclose()
                repository.close()
                logger.info("event=app_stop")

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
    application.include_router(admin_router)
    application.include_router(webdav_router)

    frontend_dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if frontend_dist.is_dir():
        application.mount(
            "/admin",
            StaticFiles(directory=frontend_dist, html=True),
            name="admin-ui",
        )

    @application.exception_handler(ResolverError)
    async def resolver_error_handler(
        request: Request,
        exc: ResolverError,
    ) -> JSONResponse:
        logger.warning(
            "event=resolver_error request=%s detail=%s",
            safe_url_for_log(str(request.url)),
            exc,
        )
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @application.exception_handler(httpx.HTTPError)
    async def upstream_error_handler(
        request: Request,
        exc: httpx.HTTPError,
    ) -> JSONResponse:
        logger.warning(
            "event=upstream_error request=%s upstream=%s",
            safe_url_for_log(str(request.url)),
            describe_http_error(exc),
        )
        return JSONResponse(
            status_code=502,
            content={
                "detail": f"Upstream request failed: {describe_http_error(exc)}"
            },
        )

    return application


default_settings = AppSettings.from_environment()
app = create_app(default_settings)


def run() -> None:
    uvicorn.run(
        app,
        host=default_settings.host,
        port=default_settings.port,
        log_level=default_settings.log_level.lower(),
        reload=False,
    )
