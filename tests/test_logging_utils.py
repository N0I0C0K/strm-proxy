import logging
from pathlib import Path

import httpx

from strm_proxy.logging_utils import (
    RUN_ID,
    configure_logging,
    describe_http_error,
    safe_url_for_log,
)


def test_safe_url_for_log_removes_credentials_query_and_fragment() -> None:
    safe = safe_url_for_log(
        "https://viewer:password@cdn.example:8443/video.mp4"
        "?token=secret#fragment"
    )

    assert safe == "https://cdn.example:8443/video.mp4"
    assert "viewer" not in safe
    assert "password" not in safe
    assert "secret" not in safe


def test_safe_url_for_log_handles_invalid_port() -> None:
    assert safe_url_for_log("https://example.com:invalid/media.ts") == "invalid-url"


def test_describe_http_error_removes_signed_query() -> None:
    request = httpx.Request(
        "GET",
        "https://www.xlys02.com/lines?t=123&sg=secret-signature",
    )
    response = httpx.Response(403, request=request)
    error = httpx.HTTPStatusError(
        "forbidden",
        request=request,
        response=response,
    )

    description = describe_http_error(error)

    assert description == "HTTP 403 from https://www.xlys02.com/lines"
    assert "secret-signature" not in description


def test_logging_suppresses_httpx_full_request_urls() -> None:
    try:
        configure_logging("DEBUG")

        assert logging.getLogger("strm_proxy").level == logging.DEBUG
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
    finally:
        configure_logging("INFO")


def test_logging_removes_queries_from_uvicorn_access_records() -> None:
    configure_logging("INFO")
    access_logger = logging.getLogger("uvicorn.access")
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "192.168.1.13:1234",
            "GET",
            "/segment?url=https%3A%2F%2Fcdn%2Fvideo&token=secret",
            "1.1",
            200,
        ),
        None,
    )

    for item in access_logger.filters:
        assert item.filter(record) is True

    assert record.getMessage() == (
        '192.168.1.13:1234 - "GET /segment HTTP/1.1" 200'
    )
    assert "secret" not in record.getMessage()


def test_logging_writes_application_and_access_records_to_file() -> None:
    log_file = Path("data/test-strm-proxy.log").resolve()
    log_file.unlink(missing_ok=True)
    try:
        configure_logging("INFO", str(log_file))
        logging.getLogger("strm_proxy.test").info("event=file_log_test")
        logging.getLogger("uvicorn.access").info("event=access_log_test")
        for handler in logging.getLogger().handlers:
            handler.flush()

        content = log_file.read_text(encoding="utf-8")
    finally:
        configure_logging("INFO", None)
        log_file.unlink(missing_ok=True)

    assert "event=file_log_test" in content
    assert "event=access_log_test" in content
    assert f"run={RUN_ID}" in content
