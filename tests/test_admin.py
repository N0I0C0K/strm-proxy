import asyncio
import base64
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from strm_proxy.app import create_app
from strm_proxy.catalog import CatalogEntry
from strm_proxy.config import AppSettings
from strm_proxy.dependencies import get_app_services, get_http_client


def _auth() -> dict[str, str]:
    token = base64.b64encode(b"demo:demo").decode()
    return {"Authorization": f"Basic {token}"}


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
