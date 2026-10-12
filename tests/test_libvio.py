import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import httpx
import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from fastapi.testclient import TestClient

from strm_proxy.app import create_app
from strm_proxy.config import AppSettings, DavSettings
from strm_proxy.catalog import CatalogEntry
from strm_proxy.database import MoviePolicy, create_media_repository
from strm_proxy.dependencies import get_app_services, get_libvio_resolver
from strm_proxy.libvio import DECRYPT_SALT, LibvioResolver, parse_libvio_detail, validate_page_url
from strm_proxy.models import ResolverError


DETAIL_URL = "https://www.libvio.lat/detail/5813847.html"
PLAY_URL = "https://www.libvio.lat/play/5813847-4-1.html"
MEDIA_URL = "https://lf26-imcloud-file-sign.bytetos.com/movie.mp4?signature=test"
TIMESTAMP = "1791690000"
MP4_PREFIX = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 52
DETAIL = """<h1 class="title">测试电影</h1>
<h3>夸克下载</h3><ul class="stui-content__playlist">
<li><a href="/play/5813847-1-1.html">下载</a></li></ul>
<h3>HD7播放</h3><ul class="stui-content__playlist">
<li><a href="/play/5813847-4-1.html">第一集</a></li>
<li><a href="/play/5813847-4-2.html">第二集</a></li></ul>"""
METADATA = """<img class="lazyload" data-original="https://img.example/poster.jpg">
<p class="data">年份：2026 / 上映：2026-07-29</p>
<p class="data">总集数：0</p>
<p class="data">评分：<span class="douban">7.7</span></p>
<p class="data">最后更新：<a>2026-10-05 23:51:51</a></p>"""
MOVIE_DETAIL = DETAIL.replace('<li><a href="/play/5813847-4-2.html">第二集</a></li>', "") + METADATA


def encrypted_url(url: str) -> str:
    digest = hashlib.md5((TIMESTAMP + DECRYPT_SALT).encode()).hexdigest()
    cipher = AES.new(digest[16:].encode(), AES.MODE_CBC, digest[:16].encode())
    return base64.b64encode(cipher.encrypt(pad(url.encode(), AES.block_size))).decode()


class Site:
    def __init__(self, *, tls_error=None, media_url=MEDIA_URL, detail=DETAIL):
        self.tls_error = tls_error
        self.media_url = media_url
        self.detail = detail
        self.requests = []
        self.media_requests = []
        self.media_status = 206
        self.api_calls = 0

    def control(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "hd.ticktockwow.com":
            self.api_calls += 1
            if self.tls_error and request.url.scheme == "https":
                raise httpx.ConnectError(self.tls_error, request=request)
            assert request.method == "POST"
            body = json.loads(request.content)
            assert body["vkey"] == "public-vkey" and body["code"] == "public-code"
            assert body["signature"] == hashlib.md5(str(body["t"]).encode()).hexdigest()
            assert request.headers["origin"] == "https://www.libvio.lat"
            return httpx.Response(200, json={"code": 200, "url": encrypted_url(self.media_url)})
        if path == "/detail/5813847.html":
            return httpx.Response(200, text=self.detail)
        if path.startswith("/play/5813847-4-"):
            # The private player ID deliberately differs from the public URL.
            return httpx.Response(
                200,
                text="var player_aaaa="
                + json.dumps(
                    {
                        "id": "3847",
                        "from": "BBA",
                        "url": "encrypted-vkey",
                        "encrypt": 3,
                    }
                )
                + ";",
            )
        if path == "/static/player/BBA.js":
            return httpx.Response(200, text='iframe.src="/static/player/artplayer/";')
        if path == "/static/player/artplayer/":
            assert request.url.params["url"] == "encrypted-vkey"
            return httpx.Response(
                200,
                text=f'''const isSmartPlay = true;
const timestamp = "{TIMESTAMP}";
const playPageUrl = "public-vkey";
const secretKeySeed = "public-code";''',
            )
        raise AssertionError(f"Unexpected site request: {request.url}")

    def media(self, request: httpx.Request) -> httpx.Response:
        self.media_requests.append(request)
        assert str(request.url) == self.media_url
        assert request.headers["range"] == "bytes=0-63"
        return httpx.Response(
            self.media_status, content=MP4_PREFIX, headers={"Content-Type": "image/jpeg"}
        )

    def resolver(self):
        return LibvioResolver(
            httpx.AsyncClient(transport=httpx.MockTransport(self.control), trust_env=False),
            httpx.AsyncClient(transport=httpx.MockTransport(self.media), trust_env=False),
        )


def test_automatic_resolution_skips_downloads_and_uses_public_id():
    async def check():
        site = Site(tls_error="[SSL: WRONG_VERSION_NUMBER] wrong version number")
        resolver = site.resolver()
        async with resolver.client, resolver.media_client:
            result = await resolver.resolve(DETAIL_URL)
            assert result.media_url == MEDIA_URL
            assert result.public_media_id == "5813847"
            assert result.play_page_url == PLAY_URL
            assert result.route_name == "HD7播放"
            assert result.lookup_scheme == "http"
            assert result.episode_number == 1
            calls = len(site.requests)
            assert await resolver.resolve(DETAIL_URL) == result
            assert len(site.requests) == calls
            assert len(site.media_requests) == 2  # Cache hits recheck signed URLs.
            assert site.api_calls == 2  # HTTPS attempt and the specific HTTP fallback.
            assert not any("-1-1.html" in str(r.url) for r in site.requests)

    asyncio.run(check())


def test_certificate_errors_do_not_trigger_http_fallback():
    async def check():
        site = Site(tls_error="[SSL: CERTIFICATE_VERIFY_FAILED]")
        resolver = site.resolver()
        async with resolver.client, resolver.media_client:
            with pytest.raises(ResolverError, match="online routes failed"):
                await resolver.resolve(DETAIL_URL)
        assert site.api_calls == 1
        assert not site.media_requests
        assert all(r.url.scheme == "https" for r in site.requests)

    asyncio.run(check())


@pytest.mark.parametrize("payload", [[], {"code": 200, "url": 42}])
def test_malformed_lookup_response_reports_route_failure(payload):
    async def check():
        site = Site()

        def control(request):
            if request.url.host == "hd.ticktockwow.com":
                return httpx.Response(200, json=payload)
            return site.control(request)

        resolver = LibvioResolver(
            httpx.AsyncClient(transport=httpx.MockTransport(control)),
            httpx.AsyncClient(transport=httpx.MockTransport(site.media)),
        )
        async with resolver.client, resolver.media_client:
            with pytest.raises(ResolverError, match="online routes failed"):
                await resolver.resolve(DETAIL_URL)
        assert not site.media_requests

    asyncio.run(check())


def test_exact_play_page_resolves_requested_episode():
    async def check():
        site = Site()
        resolver = site.resolver()
        async with resolver.client, resolver.media_client:
            result = await resolver.resolve(PLAY_URL.replace("-4-1", "-4-2"))
        assert result.episode_number == 2 and result.play_page_url.endswith("-4-2.html")
        assert not any(r.url.path == "/play/5813847-4-1.html" for r in site.requests)

    asyncio.run(check())


@pytest.mark.parametrize(
    "media_url",
    [
        "http://lf26-imcloud-file-sign.bytetos.com/movie.mp4",
        "https://127.0.0.1/private",
        "https://bytetos.com.evil.example/movie.mp4",
        "https://user:password@lf26-imcloud-file-sign.bytetos.com/movie.mp4",
    ],
)
def test_invalid_media_targets_are_never_requested(media_url):
    async def check():
        site = Site(media_url=media_url)
        resolver = site.resolver()
        async with resolver.client, resolver.media_client:
            with pytest.raises(ResolverError):
                await resolver.resolve(DETAIL_URL)
        assert not site.media_requests

    asyncio.run(check())


def test_failed_primary_route_tries_next_online_route():
    async def check():
        site = Site(
            detail=DETAIL.replace(
                "<h3>HD7播放</h3>",
                """
<h3>失效线路</h3><ul class="stui-content__playlist">
<li><a href="/play/5813847-9-1.html">第一集</a></li></ul><h3>HD7播放</h3>""",
            )
        )
        control = site.control

        def handler(request):
            if request.url.path == "/play/5813847-9-1.html":
                return httpx.Response(200, text="player unavailable")
            return control(request)

        resolver = LibvioResolver(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            httpx.AsyncClient(transport=httpx.MockTransport(site.media)),
        )
        async with resolver.client, resolver.media_client:
            assert (await resolver.resolve(DETAIL_URL)).route_name == "HD7播放"

    asyncio.run(check())


def test_stale_cached_signature_is_resolved_again():
    async def check():
        site = Site()
        resolver = site.resolver()
        media_handler = site.media

        def handler(request):
            response = media_handler(request)
            return httpx.Response(403) if len(site.media_requests) == 2 else response

        resolver.media_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with resolver.client, resolver.media_client:
            await resolver.resolve(DETAIL_URL)
            await resolver.resolve(DETAIL_URL)
        assert site.api_calls == 2
        assert len(site.media_requests) == 3

    asyncio.run(check())


@pytest.mark.parametrize(
    "url",
    [
        "http://www.libvio.lat/detail/5813847.html",
        "https://www.libvio.lat/detail/5813847.html?redirect=foo",
        "https://www.libvio.lat:444/detail/5813847.html",
        "https://www.libvio.lat/play/3847-0.htm",
        "https://evil.example/detail/5813847.html",
    ],
)
def test_page_url_validation(url):
    with pytest.raises(ResolverError):
        validate_page_url(url)


def test_webdav_strm_to_direct_playback_and_resolve():
    site = Site()
    resolver = site.resolver()
    application = create_app(
        AppSettings(
            database_path=":memory:",
            log_file=None,
            dav=DavSettings(filename="LIBVIO.strm", page_url=DETAIL_URL),
        )
    )
    application.dependency_overrides[get_libvio_resolver] = lambda: resolver
    try:
        with TestClient(application) as client:
            dav = client.get("/dav/LIBVIO.strm", auth=("demo", "demo"))
            assert dav.status_code == 200
            stable_url = dav.text.strip()
            assert parse_qs(urlsplit(stable_url).query)["page_url"] == [DETAIL_URL]
            assert "signature" not in stable_url
            play = client.get(stable_url, follow_redirects=False)
            assert play.status_code == 302 and play.headers["location"] == MEDIA_URL
            assert play.headers["cache-control"] == "no-store"
            result = client.get("/resolve", params={"page_url": DETAIL_URL})
            assert result.status_code == 200 and result.json()["provider"] == "libvio"
            assert result.json()["media_url"] == MEDIA_URL
            assert client.get("/strm", params={"page_url": DETAIL_URL}).text == dav.text
            assert (
                client.get("/play", params={"page_url": DETAIL_URL, "source": "hls"}).status_code
                == 400
            )
    finally:
        asyncio.run(resolver.client.aclose())
        asyncio.run(resolver.media_client.aclose())


def test_manual_detail_metadata_and_episode_numbers():
    movie = parse_libvio_detail(MOVIE_DETAIL, source_url=DETAIL_URL)
    assert movie.kind == "movie" and movie.year == 2026
    assert movie.public_media_id == "5813847" and movie.douban_rating == 7.7
    assert movie.cover_url == "https://img.example/poster.jpg"
    assert movie.source_updated_on == "2026-10-05"
    assert len(movie.episodes) == 1 and movie.episodes[0].play_path.endswith("-4-1.html")
    series = parse_libvio_detail(
        DETAIL + METADATA.replace("总集数：0", "总集数：12"), source_url=DETAIL_URL
    )
    assert series.kind == "series" and series.declared_episode_count == 12
    assert [episode.source_index for episode in series.episodes] == [0, 1]
    with pytest.raises(ResolverError):
        parse_libvio_detail(DETAIL.replace("HD7播放", "视频下载"), source_url=DETAIL_URL)


def test_manual_import_keeps_source_namespaces_and_survives_restart(tmp_path):
    path = str(tmp_path / "media.db")
    repository = create_media_repository(path)
    repository.import_discovered_movies(
        (
            CatalogEntry(
                xlys_id=5813847,
                title="测试电影",
                year=2026,
                cover_url=None,
                source_updated_on="2026-10-05",
                dav_filename="测试电影 (2026).strm",
            ),
        )
    )
    detail = parse_libvio_detail(MOVIE_DETAIL, source_url=DETAIL_URL)
    imported = repository.import_libvio_detail(detail)
    assert imported.xlys_id < 0 and imported.source_media_id == "5813847"
    assert imported.dav_name == "测试电影 (2026) [libvio-5813847].strm"
    assert repository.import_libvio_detail(detail).xlys_id == imported.xlys_id
    assert len(repository.list_media()) == 2
    repository.set_media_policy(imported.xlys_id, MoviePolicy.AUTO)
    repository.import_discovered_movies(
        (
            CatalogEntry(
                xlys_id=27078,
                title="另一部电影",
                year=2026,
                cover_url=None,
                source_updated_on="2026-10-06",
                dav_filename="另一部电影.strm",
            ),
        ),
        replace_auto=True,
    )
    assert repository.get_media(imported.xlys_id) is not None
    repository.close()
    reopened = create_media_repository(path)
    try:
        stored = reopened.get_media(imported.xlys_id)
        assert stored.provider == "libvio" and stored.source_url == DETAIL_URL
        assert stored.dav_name == imported.dav_name
        assert reopened.import_libvio_detail(detail).xlys_id == imported.xlys_id
    finally:
        reopened.close()


def test_manual_import_is_authenticated_and_visible_in_catalog_webdav():
    site = Site(detail=MOVIE_DETAIL)
    resolver = site.resolver()
    application = create_app(AppSettings(database_path=":memory:", log_file=None))
    application.dependency_overrides[get_libvio_resolver] = lambda: resolver
    try:
        with TestClient(application, base_url="http://192.168.50.154:8787") as client:
            auth = ("demo", "demo")
            assert client.post("/api/admin/import", json={"url": DETAIL_URL}).status_code == 401
            response = client.post("/api/admin/import", auth=auth, json={"url": DETAIL_URL})
            assert response.status_code == 200
            imported = response.json()
            assert imported["provider"] == "libvio" and imported["source_media_id"] == "5813847"
            media_id = imported["xlys_id"]
            movie = imported["catalog"]["movies"][0]
            assert movie["play_page_url"] == DETAIL_URL and movie["policy"] == "keep"
            duplicate = client.post("/api/admin/import", auth=auth, json={"url": DETAIL_URL})
            assert duplicate.json()["xlys_id"] == media_id
            assert duplicate.json()["catalog"]["counts"]["total"] == 1
            listing = client.get("/api/admin/media", auth=auth)
            assert (
                listing.status_code == 200 and listing.json()["movies"][0]["provider"] == "libvio"
            )
            dav = client.request("PROPFIND", "/dav/电影/", auth=auth, headers={"Depth": "1"})
            assert dav.status_code == 207
            hrefs = [
                item.text for item in ElementTree.fromstring(dav.content).findall(".//{DAV:}href")
            ]
            file_path = next(href for href in hrefs if href.endswith(".strm"))
            strm = client.get(file_path, auth=auth)
            assert strm.status_code == 200 and strm.text.startswith(
                "http://192.168.50.154:8787/play?"
            )
            assert parse_qs(urlsplit(strm.text.strip()).query)["page_url"] == [DETAIL_URL]
            play = client.get(strm.text.strip(), follow_redirects=False)
            assert play.status_code == 302 and play.headers["location"] == MEDIA_URL
            repository = get_app_services(application).media_repository
            old_name = repository.get_media(media_id).dav_name
            repository.set_media_policy(media_id, MoviePolicy.HIDDEN)
            site.detail = MOVIE_DETAIL.replace("测试电影", "电影新版")
            refreshed = client.post(f"/api/admin/media/{media_id}/refresh", auth=auth)
            assert refreshed.status_code == 200
            stored = repository.get_media(media_id)
            assert (
                stored.title == "电影新版"
                and stored.policy == "hidden"
                and stored.dav_name == old_name
            )
            assert client.get(file_path, auth=auth).status_code == 404
    finally:
        asyncio.run(resolver.client.aclose())
        asyncio.run(resolver.media_client.aclose())


def test_libvio_series_import_refresh_and_watched_tracking():
    site = Site(detail=DETAIL + METADATA.replace("总集数：0", "总集数：12"))
    resolver = site.resolver()
    application = create_app(AppSettings(database_path=":memory:", log_file=None))
    application.dependency_overrides[get_libvio_resolver] = lambda: resolver
    try:
        with TestClient(application) as client:
            auth = ("demo", "demo")
            imported = client.post("/api/admin/import", auth=auth, json={"url": DETAIL_URL}).json()
            assert imported["kind"] == "series" and imported["available_episode_count"] == 2
            media_id = imported["xlys_id"]
            detail = client.get(f"/api/admin/media/{media_id}", auth=auth).json()
            second = detail["episodes"][1]["play_page_url"]
            assert second == PLAY_URL.replace("-4-1", "-4-2")
            repository = get_app_services(application).media_repository
            old_name = repository.get_media(media_id).dav_name
            assert repository.dav_revisions((f"series:{media_id}",))
            assert (
                client.head(
                    "/play", params={"page_url": second}, follow_redirects=False
                ).status_code
                == 302
            )
            assert repository.get_media(media_id).last_watched_at is None
            assert (
                client.get("/play", params={"page_url": second}, follow_redirects=False).status_code
                == 302
            )
            assert repository.get_media(media_id).last_watched_at is not None
            site.detail = site.detail.replace(
                "</li></ul>", '</li><li><a href="/play/5813847-4-3.html">第三集</a></li></ul>'
            )
            refreshed = client.post("/api/admin/media/refresh-recent-series", auth=auth)
            assert refreshed.status_code == 200 and refreshed.json()["failures"] == []
            assert refreshed.json()["added_episodes"] == 1
            assert len(repository.list_episodes(media_id)) == 3
            assert repository.get_media(media_id).dav_name == old_name
    finally:
        asyncio.run(resolver.client.aclose())
        asyncio.run(resolver.media_client.aclose())


def test_provider_columns_upgrade_existing_database(tmp_path):
    from sqlalchemy import text

    path = str(tmp_path / "legacy.db")
    repository = create_media_repository(path)
    repository.import_discovered_movies(
        (
            CatalogEntry(
                xlys_id=27078,
                title="原有影片",
                year=2025,
                cover_url=None,
                source_updated_on="2026-10-05",
                dav_filename="原有影片.strm",
            ),
        )
    )
    with repository.engine.begin() as connection:
        connection.execute(text("DROP INDEX uq_media_items_provider_source_id"))
        connection.execute(text("ALTER TABLE media_items DROP COLUMN source_media_id"))
        connection.execute(text("ALTER TABLE media_items DROP COLUMN provider"))
    repository.close()
    reopened = create_media_repository(path)
    try:
        existing = reopened.get_media(27078)
        assert existing.provider == "xlys" and existing.source_media_id == "27078"
        assert existing.title == "原有影片" and existing.dav_name == "原有影片.strm"
        assert (
            reopened.import_libvio_detail(
                parse_libvio_detail(MOVIE_DETAIL, source_url=DETAIL_URL)
            ).provider
            == "libvio"
        )
    finally:
        reopened.close()
