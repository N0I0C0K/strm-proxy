import base64
from urllib.parse import quote

from fastapi.testclient import TestClient

from strm_proxy.app import create_app
from strm_proxy.catalog import CatalogMovie
from strm_proxy.dependencies import get_app_services
from strm_proxy.config import AppSettings


def _app():
    return create_app(AppSettings(database_path=":memory:"))


def _auth() -> dict[str, str]:
    token = base64.b64encode(b"demo:demo").decode()
    return {"Authorization": f"Basic {token}"}


def test_webdav_requires_authentication() -> None:
    with TestClient(_app()) as client:
        response = client.request("PROPFIND", "/dav/", headers={"Depth": "1"})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == 'Basic realm="strm-proxy"'


def test_webdav_root_lists_media_categories_and_keeps_legacy_strm_direct() -> None:
    with TestClient(_app(), base_url="http://192.168.1.20:8787") as client:
        options = client.options("/dav/")
        assert options.status_code == 200
        assert options.headers["dav"] == "1"

        listing = client.request(
            "PROPFIND",
            "/dav/",
            headers={**_auth(), "Depth": "1"},
        )
        assert listing.status_code == 207
        assert "电影" in listing.text
        assert "%E7%94%B5%E5%BD%B1/" in listing.text
        assert "电视剧" in listing.text
        assert "%E7%94%B5%E8%A7%86%E5%89%A7/" in listing.text
        assert "痴迷.strm" not in listing.text

        stream_file = client.get("/dav/痴迷.strm", headers=_auth())
        assert stream_file.status_code == 200
        assert stream_file.text.startswith("http://192.168.1.20:8787/hls.m3u8?")
        assert "proxy_segments=true" in stream_file.text

        series = client.request(
            "PROPFIND",
            "/dav/%E7%94%B5%E8%A7%86%E5%89%A7/",
            headers={**_auth(), "Depth": "1"},
        )
        assert series.status_code == 207
        assert series.text.count("<D:response>") == 1


def test_webdav_movie_catalog_lists_and_serves_discovered_strm() -> None:
    movie = CatalogMovie(
        xlys_id=27078,
        title="阳光女子合唱团",
        year=2025,
        cover_url="https://img.example/27078.jpg",
        source_updated_on="2026-08-28",
        dav_filename="阳光女子合唱团 (2025).strm",
    )
    application = _app()
    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        get_app_services(application).movie_repository.import_discovered((movie,))
        listing = client.request(
            "PROPFIND",
            "/dav/%E7%94%B5%E5%BD%B1/",
            headers={**_auth(), "Depth": "1"},
        )
        assert listing.status_code == 207
        assert "阳光女子合唱团 (2025).strm" in listing.text
        assert quote(movie.dav_filename, safe="") in listing.text

        stream_file = client.get(
            "/dav/%E7%94%B5%E5%BD%B1/" + quote(movie.dav_filename, safe=""),
            headers=_auth(),
        )
        assert stream_file.status_code == 200
        assert stream_file.text.startswith("http://192.168.1.20:8787/hls.m3u8?")
        assert "27078-0.htm" in stream_file.text
