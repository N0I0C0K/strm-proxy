from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class _UvicornAccessQueryFilter(logging.Filter):
    """Remove query strings from Uvicorn's positional access-log arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            arguments = list(record.args)
            if isinstance(arguments[2], str):
                arguments[2] = arguments[2].split("?", 1)[0]
                record.args = tuple(arguments)
        return True


def configure_logging(level: str) -> None:
    """Configure application logs without replacing Uvicorn or pytest handlers."""
    numeric_level = getattr(logging, level.upper())
    logging.basicConfig(level=numeric_level, format=LOG_FORMAT)
    logging.getLogger("strm_proxy").setLevel(numeric_level)
    # HTTPX logs complete request URLs at INFO, including signed query strings.
    # Application-owned logs already expose the useful status and safe path.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    access_logger = logging.getLogger("uvicorn.access")
    if not any(
        isinstance(item, _UvicornAccessQueryFilter)
        for item in access_logger.filters
    ):
        access_logger.addFilter(_UvicornAccessQueryFilter())


def safe_url_for_log(url: str | httpx.URL) -> str:
    """Keep URL routing context while removing credentials, query and fragment."""
    try:
        parsed = urlsplit(str(url))
        host = parsed.hostname or "unknown-host"
        port = parsed.port
    except ValueError:
        return "invalid-url"
    if port is not None:
        host = f"{host}:{port}"
    path = parsed.path or "/"
    return f"{parsed.scheme or 'unknown'}://{host}{path}"


def describe_http_error(exc: httpx.HTTPError) -> str:
    """Return an actionable HTTP error description without signed query strings."""
    request = getattr(exc, "request", None)
    request_url = safe_url_for_log(request.url) if request is not None else "unknown"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from {request_url}"
    return f"{type(exc).__name__} for {request_url}"
