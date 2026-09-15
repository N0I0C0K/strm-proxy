import httpx
import pytest

from strm_proxy.app import build_xlys_cookie_jar
from strm_proxy.config import AppSettings, DavSettings


def test_settings_reject_invalid_port() -> None:
    with pytest.raises(RuntimeError):
        AppSettings(port=0).validate()


def test_settings_reject_nested_webdav_filename() -> None:
    with pytest.raises(RuntimeError):
        DavSettings(filename="../movie.strm").validate()


def test_settings_reject_nested_catalog_directory() -> None:
    with pytest.raises(RuntimeError):
        DavSettings(catalog_directory="最新/电影").validate()


def test_settings_reject_duplicate_media_directories() -> None:
    with pytest.raises(RuntimeError):
        DavSettings(catalog_directory="媒体", series_directory="媒体").validate()


def test_settings_reject_empty_database_path() -> None:
    with pytest.raises(RuntimeError):
        AppSettings(database_path=" ").validate()


def test_settings_read_log_level_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRM_PROXY_LOG_LEVEL", "debug")

    assert AppSettings.from_environment().log_level == "DEBUG"


def test_settings_read_log_file_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRM_PROXY_LOG_FILE", "logs/playback.log")

    assert AppSettings.from_environment().log_file == "logs/playback.log"


def test_empty_log_file_environment_disables_file_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRM_PROXY_LOG_FILE", "   ")

    assert AppSettings.from_environment().log_file is None


def test_settings_reject_invalid_log_level() -> None:
    with pytest.raises(RuntimeError, match="STRM_PROXY_LOG_LEVEL"):
        AppSettings(log_level="TRACE").validate()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("1", True),
        ("YES", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("NO", False),
        ("off", False),
    ],
)
def test_settings_read_proxy_segments_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("STRM_PROXY_PROXY_SEGMENTS", value)

    assert AppSettings.from_environment().proxy_segments is expected


def test_settings_reject_invalid_proxy_segments_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRM_PROXY_PROXY_SEGMENTS", "sometimes")

    with pytest.raises(RuntimeError, match="STRM_PROXY_PROXY_SEGMENTS"):
        AppSettings.from_environment()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("false", False)],
)
def test_settings_read_play_selection_cache_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("STRM_PROXY_PLAY_SELECTION_CACHE", value)

    assert AppSettings.from_environment().play_selection_cache is expected


def test_settings_reject_invalid_play_selection_cache_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRM_PROXY_PLAY_SELECTION_CACHE", "sometimes")

    with pytest.raises(RuntimeError, match="STRM_PROXY_PLAY_SELECTION_CACHE"):
        AppSettings.from_environment()


def test_segment_cache_defaults_to_ten_minutes_and_128_mb() -> None:
    settings = AppSettings()

    assert settings.segment_prefetch_seconds == 600
    assert settings.segment_cache_max_mb == 128


def test_settings_read_segment_cache_limits_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRM_PROXY_SEGMENT_PREFETCH_SECONDS", "420")
    monkeypatch.setenv("STRM_PROXY_SEGMENT_CACHE_MAX_MB", "256")

    settings = AppSettings.from_environment()

    assert settings.segment_prefetch_seconds == 420
    assert settings.segment_cache_max_mb == 256


@pytest.mark.parametrize(
    ("field", "message"),
    [
        (
            "segment_prefetch_seconds",
            "STRM_PROXY_SEGMENT_PREFETCH_SECONDS",
        ),
        ("segment_cache_max_mb", "STRM_PROXY_SEGMENT_CACHE_MAX_MB"),
    ],
)
def test_settings_reject_negative_segment_cache_limits(
    field: str,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        AppSettings(**{field: -1}).validate()


def test_settings_read_upstream_timeouts_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRM_PROXY_REQUEST_TIMEOUT", "60")
    monkeypatch.setenv("STRM_PROXY_CONNECT_TIMEOUT", "15")

    settings = AppSettings.from_environment()

    assert settings.request_timeout_seconds == 60
    assert settings.connect_timeout_seconds == 15


def test_settings_read_upstream_proxy_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "STRM_PROXY_UPSTREAM_PROXY",
        "http://127.0.0.1:7890",
    )

    assert AppSettings.from_environment().upstream_proxy == (
        "http://127.0.0.1:7890"
    )


@pytest.mark.parametrize(
    "proxy_url",
    ["127.0.0.1:7890", "ftp://127.0.0.1:7890"],
)
def test_settings_reject_invalid_upstream_proxy(proxy_url: str) -> None:
    with pytest.raises(RuntimeError, match="STRM_PROXY_UPSTREAM_PROXY"):
        AppSettings(upstream_proxy=proxy_url).validate()


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("request_timeout_seconds", "STRM_PROXY_REQUEST_TIMEOUT"),
        ("connect_timeout_seconds", "STRM_PROXY_CONNECT_TIMEOUT"),
    ],
)
def test_settings_reject_non_positive_upstream_timeouts(
    field: str,
    message: str,
) -> None:
    with pytest.raises(RuntimeError, match=message):
        AppSettings(**{field: 0}).validate()


def test_settings_read_xlys_login_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRM_PROXY_XLYS_USERNAME", "viewer")
    monkeypatch.setenv("STRM_PROXY_XLYS_PASSWORD", "secret-token")

    settings = AppSettings.from_environment()

    assert settings.xlys_username == "viewer"
    assert settings.xlys_password == "secret-token"
    assert settings.has_xlys_login is True


def test_settings_reject_partial_xlys_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRM_PROXY_XLYS_USERNAME", "viewer")
    monkeypatch.delenv("STRM_PROXY_XLYS_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="configured together"):
        AppSettings.from_environment()


def test_xlys_login_cookies_are_scoped_away_from_media_cdns() -> None:
    settings = AppSettings(
        xlys_username="viewer",
        xlys_password="secret-token",
    )
    client = httpx.Client(cookies=build_xlys_cookie_jar(settings))

    site_request = client.build_request("GET", "https://www.xlys02.com/lines")
    media_request = client.build_request("GET", "https://vod.xl01.me/a.ts")

    assert "username=viewer" in site_request.headers.get("cookie", "")
    assert "password=secret-token" in site_request.headers.get("cookie", "")
    assert "cookie" not in media_request.headers
