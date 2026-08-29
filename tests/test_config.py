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
