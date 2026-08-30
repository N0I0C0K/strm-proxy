from __future__ import annotations

import os
from dataclasses import dataclass, field


DEFAULT_PAGE_URL = "https://www.xlys02.com/play/27062-0.htm"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class DavSettings:
    username: str = "demo"
    password: str = "demo"
    filename: str = "痴迷.strm"
    page_url: str = DEFAULT_PAGE_URL
    catalog_directory: str = "电影"
    series_directory: str = "电视剧"

    def validate(self) -> None:
        if (
            "/" in self.filename
            or "\\" in self.filename
            or not self.filename.lower().endswith(".strm")
        ):
            raise RuntimeError("WebDAV filename must be one .strm filename")
        if (
            not self.catalog_directory.strip()
            or "/" in self.catalog_directory
            or "\\" in self.catalog_directory
        ):
            raise RuntimeError("WebDAV catalog directory must be one directory name")
        if (
            not self.series_directory.strip()
            or "/" in self.series_directory
            or "\\" in self.series_directory
        ):
            raise RuntimeError("WebDAV series directory must be one directory name")
        if self.catalog_directory.casefold() == self.series_directory.casefold():
            raise RuntimeError("WebDAV movie and series directories must be different")


@dataclass(frozen=True, slots=True)
class AppSettings:
    host: str = "0.0.0.0"
    port: int = 8787
    proxy_segments: bool = True
    user_agent: str = DEFAULT_USER_AGENT
    request_timeout_seconds: float = 20.0
    connect_timeout_seconds: float = 10.0
    cache_ttl_seconds: float = 300.0
    discovery_recent_limit: int = 50
    discovery_year_span: int = 4
    douban_rating_threshold: float = 7.0
    series_douban_rating_threshold: float = 8.0
    catalog_cache_ttl_seconds: float = 1800.0
    database_path: str = "data/strm-proxy.db"
    allowed_page_hosts: tuple[str, ...] = ("www.xlys02.com", "xlys02.com")
    segment_host: str = "vod.xl01.me"
    dav: DavSettings = field(default_factory=DavSettings)

    @classmethod
    def from_environment(cls) -> "AppSettings":
        settings = cls(
            host=os.getenv("STRM_PROXY_HOST", "0.0.0.0"),
            port=int(os.getenv("STRM_PROXY_PORT", "8787")),
            proxy_segments=_environment_bool(
                "STRM_PROXY_PROXY_SEGMENTS",
                default=True,
            ),
            cache_ttl_seconds=float(os.getenv("STRM_PROXY_CACHE_TTL", "300")),
            discovery_recent_limit=int(
                os.getenv("STRM_PROXY_DISCOVERY_RECENT_LIMIT", "50")
            ),
            discovery_year_span=int(
                os.getenv("STRM_PROXY_DISCOVERY_YEAR_SPAN", "4")
            ),
            douban_rating_threshold=float(
                os.getenv("STRM_PROXY_DOUBAN_RATING_THRESHOLD", "7.0")
            ),
            series_douban_rating_threshold=float(
                os.getenv("STRM_PROXY_SERIES_DOUBAN_RATING_THRESHOLD", "8.0")
            ),
            catalog_cache_ttl_seconds=float(
                os.getenv("STRM_PROXY_CATALOG_CACHE_TTL", "1800")
            ),
            database_path=os.getenv(
                "STRM_PROXY_DATABASE_PATH", "data/strm-proxy.db"
            ),
            dav=DavSettings(
                username=os.getenv("STRM_PROXY_DAV_USER", "demo"),
                password=os.getenv("STRM_PROXY_DAV_PASSWORD", "demo"),
                filename=os.getenv("STRM_PROXY_DAV_FILENAME", "痴迷.strm"),
                page_url=os.getenv("STRM_PROXY_PAGE_URL", DEFAULT_PAGE_URL),
                catalog_directory=os.getenv(
                    "STRM_PROXY_DAV_CATALOG_DIRECTORY", "电影"
                ),
                series_directory=os.getenv(
                    "STRM_PROXY_DAV_SERIES_DIRECTORY", "电视剧"
                ),
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise RuntimeError("STRM_PROXY_PORT must be between 1 and 65535")
        if self.cache_ttl_seconds <= 0:
            raise RuntimeError("STRM_PROXY_CACHE_TTL must be positive")
        if not 1 <= self.discovery_recent_limit <= 500:
            raise RuntimeError(
                "STRM_PROXY_DISCOVERY_RECENT_LIMIT must be between 1 and 500"
            )
        if not 1 <= self.discovery_year_span <= 10:
            raise RuntimeError(
                "STRM_PROXY_DISCOVERY_YEAR_SPAN must be between 1 and 10"
            )
        if not 0 <= self.douban_rating_threshold <= 10:
            raise RuntimeError(
                "STRM_PROXY_DOUBAN_RATING_THRESHOLD must be between 0 and 10"
            )
        if not 0 <= self.series_douban_rating_threshold <= 10:
            raise RuntimeError(
                "STRM_PROXY_SERIES_DOUBAN_RATING_THRESHOLD must be between 0 and 10"
            )
        if self.catalog_cache_ttl_seconds <= 0:
            raise RuntimeError("STRM_PROXY_CATALOG_CACHE_TTL must be positive")
        if not self.database_path.strip():
            raise RuntimeError("STRM_PROXY_DATABASE_PATH must not be empty")
        self.dav.validate()


def _environment_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(
        f"{name} must be one of true/false, 1/0, yes/no, or on/off"
    )
