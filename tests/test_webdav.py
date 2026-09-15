import base64
from urllib.parse import parse_qs, quote, urlparse

from fastapi.testclient import TestClient

from strm_proxy.app import create_app
from strm_proxy.catalog import CatalogEntry
from strm_proxy.dependencies import get_app_services, get_resolver
from strm_proxy.config import AppSettings
from strm_proxy.detail import parse_xlys_detail
from strm_proxy.models import ResolvedPage, StreamCandidate


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
        assert stream_file.text.startswith("http://192.168.1.20:8787/play?")
        assert "proxy_segments" not in stream_file.text
        assert "source=" not in stream_file.text

def test_webdav_movie_catalog_lists_and_serves_discovered_strm() -> None:
    movie = CatalogEntry(
        xlys_id=27078,
        title="阳光女子合唱团",
        year=2025,
        cover_url="https://img.example/27078.jpg",
        source_updated_on="2026-08-28",
        dav_filename="阳光女子合唱团 (2025).strm",
    )
    application = _app()
    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        get_app_services(application).media_repository.import_discovered_movies((movie,))
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
        assert stream_file.text.startswith("http://192.168.1.20:8787/play?")
        assert "source=" not in stream_file.text
        assert "27078-0.htm" in stream_file.text


def test_webdav_to_versioned_hls_manifest_end_to_end() -> None:
    class HlsResolver:
        allowed_hosts = ("www.xlys02.com", "xlys02.com")
        candidate = StreamCandidate(
            kind="m3u8_2",
            url="https://vod.xl01.me/current.m3u8#iplay",
        )

        async def resolve_page(self, page_url: str) -> ResolvedPage:
            return ResolvedPage(
                page_url=page_url,
                pid=204486,
                title="痴迷",
                candidates=(self.candidate,),
            )

        async def find_working_hls_line(
            self,
            page_url: str,
            preferred_line: int = 0,
        ) -> int:
            return 0

        async def fetch_manifest(self, page_url: str, line: int = 0):
            return (
                await self.resolve_page(page_url),
                self.candidate,
                "#EXTM3U\n#EXTINF:6,\nhttps://vod.xl01.me/current.ts\n",
            )

    application = create_app(
        AppSettings(database_path=":memory:", proxy_segments=False)
    )
    application.dependency_overrides[get_resolver] = HlsResolver

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        stream_file = client.get("/dav/痴迷.strm", headers=_auth())
        play = client.get(
            stream_file.text.strip(),
            follow_redirects=False,
        )
        manifest_url = play.headers["location"]
        current = client.get(manifest_url, follow_redirects=False)
        old = client.get(
            "/hls.m3u8",
            params={
                "page_url": "https://www.xlys02.com/play/27062-0.htm",
                "v": "old",
            },
            follow_redirects=False,
        )

    assert stream_file.status_code == 200
    assert play.status_code == 302
    assert parse_qs(urlparse(manifest_url).query)["v"][0]
    assert current.status_code == 200
    assert current.text.startswith("#EXTM3U")
    assert old.status_code == 302
    assert old.headers["location"] == manifest_url


def test_webdav_series_catalog_lists_season_and_episode_strm() -> None:
    detail = parse_xlys_detail(
        """
        <div class="movie-header">
          <div class="movie-poster"><img src="https://img.example/show.jpg"></div>
          <h1 class="movie-title">柯蒂斯总统 第一季 (2026)</h1>
          <div class="info-item"><span class="info-label">集数：</span><span class="info-value">10</span></div>
        </div>
        <a class="play-item" href="/play/27085-0.htm">第1集</a>
        <a class="play-item" href="/play/27085-1.htm">第2集</a>
        """,
        source_url="https://www.xlys02.com/meiju/27085.htm",
        source_updated_on="2026-08-28",
    )
    entry = CatalogEntry(
        xlys_id=27085,
        title=detail.title,
        year=detail.year,
        cover_url=detail.cover_url,
        source_updated_on="2026-08-28",
        dav_filename="柯蒂斯总统 第一季 (2026).strm",
        source_url=detail.source_url,
        douban_rating=8.1,
    )
    application = _app()
    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        get_app_services(application).media_repository.import_discovered_series(
            (entry,),
            (detail,),
        )
        series_name = quote("柯蒂斯总统 第一季 (2026)", safe="")
        root = client.request(
            "PROPFIND",
            "/dav/%E7%94%B5%E8%A7%86%E5%89%A7/",
            headers={**_auth(), "Depth": "1"},
        )
        assert root.status_code == 207
        assert series_name in root.text

        show = client.request(
            "PROPFIND",
            f"/dav/%E7%94%B5%E8%A7%86%E5%89%A7/{series_name}/",
            headers={**_auth(), "Depth": "1"},
        )
        assert "Season%2001/" in show.text

        season = client.request(
            "PROPFIND",
            f"/dav/%E7%94%B5%E8%A7%86%E5%89%A7/{series_name}/Season%2001/",
            headers={**_auth(), "Depth": "1"},
        )
        assert "S01E01.strm" in season.text
        assert "S01E02.strm" in season.text

        episode_name = quote("柯蒂斯总统 第一季 S01E01.strm", safe="")
        episode = client.get(
            f"/dav/%E7%94%B5%E8%A7%86%E5%89%A7/{series_name}/Season%2001/{episode_name}",
            headers=_auth(),
        )
        assert episode.status_code == 200
        assert "27085-0.htm" in episode.text
