import pytest

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
