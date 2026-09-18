import asyncio
import base64
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from strm_proxy.app import create_app
from strm_proxy.catalog import CatalogEntry
from strm_proxy.config import AppSettings, DavSettings
from strm_proxy.database import MoviePolicy
from strm_proxy.dependencies import (
    get_app_services,
    get_http_client,
    get_playback,
    get_resolver,
)
from strm_proxy.detail import XlysDetail, XlysEpisode
from strm_proxy.models import ResolvedPage, StreamCandidate


def _auth() -> dict[str, str]:
    token = base64.b64encode(b"demo:demo").decode()
    return {"Authorization": f"Basic {token}"}


def _credentials(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_credentials_change_updates_admin_and_webdav_and_survives_restart(tmp_path) -> None:
    database_path = tmp_path / "credentials.db"
    application = create_app(AppSettings(database_path=str(database_path)))
    with TestClient(application) as client:
        assert client.get("/api/admin/credentials", headers=_auth()).json() == {"username": "demo"}
        rejected = client.put(
            "/api/admin/credentials", headers=_auth(),
            json={"username": "media", "current_password": "wrong", "password": "new-secret-123"},
        )
        assert rejected.status_code == 403
        response = client.put(
            "/api/admin/credentials", headers=_auth(),
            json={"username": "media", "current_password": "demo", "password": "new-secret-123"},
        )
        assert response.status_code == 200
        assert response.json() == {"username": "media"}
        assert client.get("/api/admin/credentials", headers=_auth()).status_code == 401
        assert client.get("/api/admin/credentials", headers=_credentials("media", "new-secret-123")).status_code == 200
        assert client.get("/dav/", headers=_auth()).status_code == 401
        assert client.get("/dav/", headers=_credentials("media", "new-secret-123")).status_code == 200

    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT username, salt, password_hash FROM access_credentials").fetchone()
    assert row is not None
    assert row[0] == "media"
    assert len(row[1]) == 32
    assert len(row[2]) == 64
    assert "new-secret-123" not in str(row)

    restarted = create_app(AppSettings(
        database_path=str(database_path), dav=DavSettings(username="bootstrap", password="ignored")
    ))
    with TestClient(restarted) as client:
        assert client.get("/api/admin/credentials", headers=_credentials("media", "new-secret-123")).status_code == 200
        assert client.get("/api/admin/credentials", headers=_credentials("bootstrap", "ignored")).status_code == 401


def _movie() -> CatalogEntry:
    return CatalogEntry(
        xlys_id=27078,
        title="阳光女子合唱团",
        year=2025,
        cover_url="https://img.example/27078.jpg",
        source_updated_on="2026-08-28",
        dav_filename="阳光女子合唱团 (2025).strm",
    )


def _other_movie() -> CatalogEntry:
    return CatalogEntry(
        xlys_id=27079,
        title="另一部电影",
        year=2024,
        cover_url=None,
        source_updated_on="2026-08-27",
        dav_filename="另一部电影 (2024).strm",
    )


def test_admin_api_requires_authentication() -> None:
    application = create_app(AppSettings(database_path=":memory:"))
    with TestClient(application) as client:
        response = client.get("/api/admin/movies")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == (
            'Basic realm="strm-proxy admin"'
        )


def test_admin_lists_movies_and_updates_policy() -> None:
    application = create_app(AppSettings(database_path=":memory:"))
    with TestClient(application) as client:
        get_app_services(application).media_repository.import_discovered_movies((_movie(),))

        listing = client.get("/api/admin/movies", headers=_auth())
        assert listing.status_code == 200
        payload = listing.json()
        assert payload["counts"] == {
            "total": 1,
            "automatic": 1,
            "kept": 0,
            "hidden": 0,
            "movies": 1,
            "series": 0,
        }
        assert payload["movies"][0]["kind"] == "movie"
        assert payload["movies"][0]["cover_url"] == (
            "https://img.example/27078.jpg"
        )

        update = client.patch(
            "/api/admin/movies/27078/policy",
            headers=_auth(),
            json={"policy": "keep"},
        )
        assert update.status_code == 200
        assert update.json()["policy"] == "keep"
        assert (
            get_app_services(application).media_repository.get_movie(27078).policy
            == "keep"
        )


def test_admin_batch_updates_and_deletes_movies() -> None:
    application = create_app(AppSettings(database_path=":memory:"))
    with TestClient(application) as client:
        get_app_services(application).media_repository.import_discovered_movies(
            (_movie(), _other_movie())
        )

        hidden = client.patch(
            "/api/admin/movies/batch/policy",
            headers=_auth(),
            json={"xlys_ids": [27078, 27079], "policy": "hidden"},
        )
        assert hidden.status_code == 200
        assert hidden.json()["counts"]["hidden"] == 2

        deleted = client.request(
            "DELETE",
            "/api/admin/movies/batch",
            headers=_auth(),
            json={"xlys_ids": [27078]},
        )
        assert deleted.status_code == 200
        assert deleted.json()["counts"] == {
            "total": 1,
            "automatic": 0,
            "kept": 0,
            "hidden": 1,
            "movies": 1,
            "series": 0,
        }


def test_admin_can_select_named_playback_route_and_restore_auto() -> None:
    class StubResolver:
        def __init__(self) -> None:
            self.candidates = (
                StreamCandidate("m3u8", "https://cdn/slow.m3u8#inews"),
                StreamCandidate("m3u8_2", "https://cdn/fast.m3u8#iplay"),
                StreamCandidate("url3", "https://cdn/unnamed.m3u8"),
            )
            self.refreshes: list[bool] = []

        async def resolve_page(
            self,
            page_url: str,
            *,
            refresh: bool = False,
        ) -> ResolvedPage:
            self.refreshes.append(refresh)
            return ResolvedPage(
                page_url=page_url,
                pid=204486,
                title="阳光女子合唱团",
                candidates=self.candidates,
            )

    resolver = StubResolver()
    application = create_app(AppSettings(database_path=":memory:"))
    application.dependency_overrides[get_resolver] = lambda: resolver

    with TestClient(application) as client:
        services = get_app_services(application)
        services.media_repository.import_discovered_movies((_movie(),))

        routes = client.get(
            "/api/admin/media/27078/playback-routes",
            headers=_auth(),
        )
        assert routes.status_code == 200
        assert [item["name"] for item in routes.json()["routes"]] == [
            "inews",
            "iplay",
            None,
        ]
        assert routes.json()["routes"][2]["selectable"] is False

        selected = client.put(
            "/api/admin/media/27078/playback-routes",
            headers=_auth(),
            json={"route_name": "iplay"},
        )
        assert selected.status_code == 200
        assert selected.json()["selected_line"] == 1
        assert selected.json()["selected_route_name"] == "iplay"
        assert selected.json()["manual_override"] is True

        resolver.candidates = (
            StreamCandidate("url3", "https://cdn/fast-new.m3u8#iplay"),
            StreamCandidate("m3u8", "https://cdn/slow-new.m3u8#inews"),
        )
        remapped = client.get(
            "/api/admin/media/27078/playback-routes",
            headers=_auth(),
        )
        assert remapped.status_code == 200
        assert remapped.json()["selected_line"] == 0
        assert remapped.json()["selected_route_name"] == "iplay"

        cleared = client.delete(
            "/api/admin/media/27078/playback-routes",
            headers=_auth(),
        )
        assert cleared.status_code == 200
        assert cleared.json()["cleared"] is True
        assert services.playback.cache.get(
            ResolvedPage(
                page_url=cleared.json()["page_url"],
                pid=204486,
                title="阳光女子合唱团",
                candidates=resolver.candidates,
            )
        ) is None
        assert resolver.refreshes == [True, False, True]


def test_admin_manual_movie_import_keeps_movie() -> None:
    html = """
    <div class="movie-header">
      <div class="movie-poster"><img src="https://img.example/manual.jpg"></div>
      <h1 class="movie-title">手动电影 (2026)</h1>
    </div>
    <a class="play-item" href="/play/27101-0.htm">在线播放</a>
    """

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/juqing/27101.htm"
        return httpx.Response(
            200,
            text=html,
            headers={"Last-Modified": "Fri, 28 Aug 2026 10:57:11 GMT"},
        )

    application = create_app(AppSettings(database_path=":memory:"))
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    application.dependency_overrides[get_http_client] = lambda: mock_client
    try:
        with TestClient(application) as client:
            response = client.post(
                "/api/admin/import",
                headers=_auth(),
                json={"url": "https://www.xlys02.com/juqing/27101.htm"},
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["imported"] is True
            assert payload["catalog"]["counts"]["kept"] == 1
            stored = get_app_services(application).media_repository.get_movie(27101)
            assert stored is not None
            assert stored.policy == "keep"
            assert stored.source_updated_on == datetime(
                2026, 8, 28, tzinfo=timezone.utc
            ).date()
    finally:
        asyncio.run(mock_client.aclose())


def test_admin_manual_series_import_writes_series_and_episodes() -> None:
    html = """
    <div class="movie-header">
      <div class="movie-poster"><img src="https://img.example/show.jpg"></div>
      <h1 class="movie-title">柯蒂斯总统 第一季 (2026)</h1>
      <div class="info-item"><span class="info-label">集数：</span><span class="info-value">10</span></div>
    </div>
    <a class="play-item" href="/play/27085-0.htm">第1集</a>
    <a class="play-item" href="/play/27085-1.htm">第2集</a>
    """

    application = create_app(AppSettings(database_path=":memory:"))
    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=html))
    )
    application.dependency_overrides[get_http_client] = lambda: mock_client
    try:
        with TestClient(application) as client:
            response = client.post(
                "/api/admin/import",
                headers=_auth(),
                json={"url": "https://www.xlys02.com/meiju/27085.htm"},
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["kind"] == "series"
            assert payload["imported"] is True
            assert payload["available_episode_count"] == 2
            assert payload["declared_episode_count"] == 10
            assert payload["catalog"]["counts"]["series"] == 1
            assert payload["catalog"]["movies"][0]["kind"] == "series"
            repository = get_app_services(application).media_repository
            stored = repository.get_series(27085)
            assert stored is not None
            assert stored.policy == "keep"
            assert len(repository.list_episodes(27085)) == 2
    finally:
        asyncio.run(mock_client.aclose())


def _series(xlys_id: int) -> tuple[CatalogEntry, XlysDetail]:
    url = f"https://www.xlys02.com/guoju/{xlys_id}.htm"
    entry = CatalogEntry(
        xlys_id=xlys_id,
        title=f"测试剧{xlys_id}",
        year=2026,
        cover_url=None,
        source_updated_on="2026-09-01",
        dav_filename=f"测试剧{xlys_id} (2026).strm",
        source_url=url,
    )
    detail = XlysDetail(
        xlys_id=xlys_id,
        kind="series",
        category="guoju",
        title=entry.title,
        year=2026,
        season_number=1,
        cover_url=None,
        declared_episode_count=10,
        episodes=(
            XlysEpisode(0, "第1集", f"/play/{xlys_id}-0.htm"),
            XlysEpisode(1, "第2集", f"/play/{xlys_id}-1.htm"),
        ),
        source_updated_on=None,
        source_url=url,
    )
    return entry, detail


def test_admin_refreshes_one_series_and_preserves_policy() -> None:
    entry, detail = _series(27085)
    html = """
    <h1 class="movie-title">测试剧新版 第一季 (2026)</h1>
    <div class="info-item"><span class="info-label">集数：</span><span class="info-value">12</span></div>
    <a class="play-item" href="/play/27085-0.htm">第1集</a>
    <a class="play-item" href="/play/27085-2.htm">第3集</a>
    """
    application = create_app(AppSettings(database_path=":memory:"))
    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=html))
    )
    application.dependency_overrides[get_http_client] = lambda: mock_client
    try:
        with TestClient(application) as client:
            repository = get_app_services(application).media_repository
            repository.import_discovered_series((entry,), (detail,))
            repository.set_series_policy(27085, MoviePolicy.KEEP)
            old_name = repository.get_series(27085).dav_name
            unauthorized = client.post("/api/admin/media/27085/refresh")
            assert unauthorized.status_code == 401
            refreshed = client.post("/api/admin/media/27085/refresh", headers=_auth())
            assert refreshed.status_code == 200
            assert refreshed.json()["item"]["added_episodes"] == 1
            assert refreshed.json()["item"]["available_episode_count"] == 2
            stored = repository.get_series(27085)
            assert stored.title == "测试剧新版 第一季"
            assert stored.declared_episode_count == 12
            assert stored.policy == "keep"
            assert stored.dav_name == old_name
            assert [episode.source_index for episode in repository.list_episodes(27085)] == [0, 2]
            assert client.post("/api/admin/media/99999/refresh", headers=_auth()).status_code == 404
    finally:
        asyncio.run(mock_client.aclose())


def test_admin_refreshes_movie_without_changing_policy_or_dav_name() -> None:
    application = create_app(AppSettings(database_path=":memory:"))
    html = """
    <h1 class="movie-title">电影新版 (2026)</h1>
    <a class="play-item" href="/play/27078-0.htm">在线播放</a>
    """
    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=html))
    )
    application.dependency_overrides[get_http_client] = lambda: mock_client
    try:
        with TestClient(application) as client:
            repository = get_app_services(application).media_repository
            repository.import_discovered_movies((_movie(),))
            assert client.post(
                "/api/admin/media/27078/refresh", headers=_auth()
            ).status_code == 409
            repository.import_discovered_movies((CatalogEntry(
                xlys_id=27078,
                title="原名",
                year=2025,
                cover_url=None,
                source_updated_on="2026-09-01",
                dav_filename="原名 (2025).strm",
                source_url="https://www.xlys02.com/juqing/27078.htm",
            ),))
            repository.set_movie_policy(27078, MoviePolicy.KEEP)
            original_name = repository.get_movie(27078).dav_name
            result = client.post("/api/admin/media/27078/refresh", headers=_auth())
            assert result.status_code == 200
            assert result.json()["item"]["kind"] == "movie"
            assert result.json()["item"]["added_episodes"] == 0
            stored = repository.get_movie(27078)
            assert stored.title == "电影新版"
            assert stored.year == 2026
            assert stored.policy == "keep"
            assert stored.dav_name == original_name
    finally:
        asyncio.run(mock_client.aclose())


def test_recent_series_refresh_uses_successful_get_playback_only() -> None:
    first, first_detail = _series(27085)
    second, second_detail = _series(27086)
    unwatched, unwatched_detail = _series(27087)

    class Playback:
        async def resolve_auto(self, _resolver, page_url):
            return SimpleNamespace(
                selection=SimpleNamespace(source="tos"),
                resolved=SimpleNamespace(pid=1),
                media_url="https://cdn.example/movie.mp4",
                cache_hit=False,
            )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("27086.htm"):
            return httpx.Response(500)
        assert request.url.path.endswith("27085.htm")
        return httpx.Response(200, text="""
            <h1 class="movie-title">测试剧27085 (2026)</h1>
            <a class="play-item" href="/play/27085-0.htm">第1集</a>
            <a class="play-item" href="/play/27085-1.htm">第2集</a>
            <a class="play-item" href="/play/27085-2.htm">第3集</a>
        """)

    application = create_app(AppSettings(database_path=":memory:"))
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    application.dependency_overrides[get_http_client] = lambda: mock_client
    application.dependency_overrides[get_playback] = Playback
    try:
        with TestClient(application) as client:
            repository = get_app_services(application).media_repository
            repository.import_discovered_series(
                (first, second, unwatched),
                (first_detail, second_detail, unwatched_detail),
            )
            repository.import_discovered_movies((_movie(),))
            for xlys_id in (27085, 27086):
                play_url = f"https://www.xlys02.com/play/{xlys_id}-0.htm"
                assert client.head("/play", params={"page_url": play_url}, follow_redirects=False).status_code == 302
                assert repository.get_series(xlys_id).last_watched_at is None
                assert client.get("/play", params={"page_url": play_url}, follow_redirects=False).status_code == 302
            assert len(repository.recently_watched_series()) == 2
            assert client.get("/api/admin/media", headers=_auth()).json()["recently_watched_series_count"] == 2
            result = client.post("/api/admin/media/refresh-recent-series", headers=_auth())
            assert result.status_code == 200
            assert result.json()["checked"] == 2
            assert result.json()["refreshed"] == 1
            assert result.json()["added_episodes"] == 1
            assert result.json()["failures"][0]["xlys_id"] == 27086
            assert len(repository.list_episodes(27085)) == 3
            assert len(repository.list_episodes(27087)) == 2
    finally:
        asyncio.run(mock_client.aclose())


def test_admin_media_detail_and_catalog_episode_counts() -> None:
    entry, detail = _series(27085)
    application = create_app(AppSettings(database_path=":memory:"))
    with TestClient(application) as client:
        repository = get_app_services(application).media_repository
        repository.import_discovered_movies((_movie(),))
        repository.import_discovered_series((entry,), (detail,))

        listing = client.get("/api/admin/media", headers=_auth())
        assert listing.status_code == 200
        cards = {item["xlys_id"]: item for item in listing.json()["movies"]}
        assert cards[27085]["available_episode_count"] == 2
        assert cards[27078]["available_episode_count"] == 0

        assert client.get("/api/admin/media/27085").status_code == 401
        response = client.get("/api/admin/media/27085", headers=_auth())
        assert response.status_code == 200
        payload = response.json()
        assert payload["source_url"] == entry.source_url
        assert payload["declared_episode_count"] == 10
        assert payload["season_number"] == 1
        assert payload["available_episode_count"] == 2
        assert [episode["source_index"] for episode in payload["episodes"]] == [0, 1]
        assert payload["episodes"][1]["play_page_url"].endswith("/play/27085-1.htm")
        assert payload["last_checked_at"] is not None

        updated = client.patch(
            "/api/admin/media/27085/policy",
            headers=_auth(),
            json={"policy": "hidden"},
        )
        assert updated.json()["available_episode_count"] == 2
        assert client.get("/api/admin/media/99999", headers=_auth()).status_code == 404
