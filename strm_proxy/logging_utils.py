from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import secrets
from urllib.parse import urlsplit

import httpx


RUN_ID = secrets.token_hex(4)
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s run=%(run_id)s %(message)s"
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5


class _UvicornAccessQueryFilter(logging.Filter):
    """Remove query strings from Uvicorn's positional access-log arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            arguments = list(record.args)
            if isinstance(arguments[2], str):
                arguments[2] = arguments[2].split("?", 1)[0]
                record.args = tuple(arguments)
        return True


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = RUN_ID
        return True


def configure_logging(level: str, file_path: str | None = None) -> None:
    """Configure application logs without replacing Uvicorn or pytest handlers."""
    numeric_level = getattr(logging, level.upper())
    logging.basicConfig(level=numeric_level, format=LOG_FORMAT)
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    _configure_file_handler(root_logger, file_path, numeric_level)
    for handler in root_logger.handlers:
        if not any(isinstance(item, _RunIdFilter) for item in handler.filters):
            handler.addFilter(_RunIdFilter())
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


def _configure_file_handler(
    logger: logging.Logger,
    file_path: str | None,
    level: int,
) -> None:
    configured_handlers = [
        handler
        for handler in logger.handlers
        if getattr(handler, "_strm_proxy_log_file", False)
    ]
    if file_path is None:
        for handler in configured_handlers:
            logger.removeHandler(handler)
            handler.close()
        return

    target = Path(file_path).resolve()
    for handler in configured_handlers:
        if Path(handler.baseFilename) == target:
            handler.setLevel(level)
            return
        logger.removeHandler(handler)
        handler.close()

    target.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        target,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler._strm_proxy_log_file = True  # type: ignore[attr-defined]
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)


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
