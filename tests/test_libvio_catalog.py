import asyncio
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from strm_proxy.app import create_app
from strm_proxy.catalog import CatalogEntry
from strm_proxy.config import AppSettings
from strm_proxy.database import MoviePolicy, create_media_repository
from strm_proxy.dependencies import get_app_services
from strm_proxy.library import MediaLibrary
from strm_proxy.libvio import parse_libvio_detail
from strm_proxy.libvio_catalog import LibvioCatalog, LibvioDiscovery, parse_libvio_list
from strm_proxy.models import ResolverError


def _detail_html(source_id, *, series=False, download=False, count=2):
    label = "视频下载" if download else "HD7播放"
    episodes = "".join(
        f'<li><a href="/play/{source_id}-4-{index}.html">{f"第{index}集" if series else "HD中字"}</a></li>'
        for index in range(1, (count if series else 1) + 1)
    )
    return (
        f'<h1 class="title">影片{source_id}</h1><p class="data">年份：2026</p>'
        f'<p class="data">类型：{"海外剧" if series else "剧情"}</p>'
        f'<p class="data">总集数：{12 if series else 0}</p>'
        f'<h3>{label}</h3><ul class="stui-content__playlist">{episodes}</ul>'
    )


def _detail(source_id, *, series=False, count=2):
    return parse_libvio_detail(
        _detail_html(source_id, series=series, count=count),
        source_url=f"https://www.libvio.lat/detail/{source_id}.html",
    )


def _listing(entries, next_path=None):
    cards = "".join(
        f'<li><a class="stui-vodlist__thumb lazyload" href="/detail/{source_id}.html" '
        f'title="影片{source_id}"><span class="pic-tag">{rating}</span></a></li>'
        for source_id, rating in entries
    )
    paging = f'<a href="{next_path}">下一页</a>' if next_path else ""
    return f'<ul class="stui-vodlist clearfix">{cards}</ul>{paging}'


def test_list_reads_cards_rating_and_next_link_ignoring_sidebar():
    parsed = parse_libvio_list(
        _listing([(5813847, 8.1)], "/show/1--time------2---.html")
        + '<a class="stui-vodlist__thumb" href="/detail/99.html">侧栏推荐</a>'
    )
    assert len(parsed.cards) == 1
    assert parsed.cards[0].source_media_id == "5813847"
    assert parsed.cards[0].rating == 8.1
    assert parsed.next_url == "https://www.libvio.lat/show/1--time------2---.html"
    with pytest.raises(ResolverError):
        parse_libvio_list("<title>请完成验证</title>")


def test_discovery_pages_recent_and_unions_rated_years_deduplicating_details():
    calls = Counter()

    def handle(request):
        path = request.url.path
        calls[path] += 1
        if path.startswith("/detail/"):
            return httpx.Response(200, text=_detail_html(path.split("/")[-1].split(".")[0]))
        if path == "/show/1--time---------.html":
            return httpx.Response(
                200, text=_listing([(900, 0), (20260, 8.4)], "/show/1--time------2---.html")
            )
        if path == "/show/1--time------2---.html":
            return httpx.Response(200, text=_listing([(902, 0), (903, 0)]))
        year = path.removesuffix(".html").split("-")[-1]
        entries = [(20260, 8.4), (20261, 7.0)] if year == "2026" else [(20250, 7.8)]
        return httpx.Response(200, text=_listing(entries))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            catalog = LibvioCatalog(
                client, recent_limit=3, year_span=2, current_year=2026, request_interval_seconds=0
            )
            first = await catalog.discover_movies()
            assert await catalog.discover_movies() is first
            return first

    result = asyncio.run(scenario())
    assert result.complete
    assert {detail.public_media_id for detail in result.details} == {"900", "902", "20260", "20250"}
    assert calls["/detail/20260.html"] == 1
    assert calls["/detail/20261.html"] == 0
    assert calls["/detail/903.html"] == 0
    assert calls["/show/1--score---------2025.html"] == 1


def test_download_only_is_excluded_but_detail_failure_marks_snapshot_incomplete_and_retries():
    fail = True
    calls = Counter()

    def handle(request):
        path = request.url.path
        calls[path] += 1
        if path.startswith("/show/"):
            return httpx.Response(200, text=_listing([(1, 0), (2, 0), (3, 0)]))
        source_id = path.split("/")[-1].split(".")[0]
        if source_id == "3" and fail:
            return httpx.Response(200, text="broken detail")
        return httpx.Response(200, text=_detail_html(source_id, download=source_id == "2"))

    async def scenario():
        nonlocal fail
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            catalog = LibvioCatalog(client, recent_limit=3, year_span=0, request_interval_seconds=0)
            first = await catalog.discover_movies()
            assert not first.complete and len(first.failures) == 1
            assert [detail.public_media_id for detail in first.details] == ["1"]
            fail = False
            return await catalog.discover_movies()

    result = asyncio.run(scenario())
    assert result.complete and {detail.public_media_id for detail in result.details} == {"1", "3"}
    assert calls["/show/1--time---------.html"] == 2


def test_series_uses_strict_above_eight_and_imports_online_episode_numbers():
    def handle(request):
        if request.url.path.startswith("/detail/"):
            return httpx.Response(
                200, text=_detail_html(request.url.path.split("/")[-1].split(".")[0], series=True)
            )
        entries = [(900, 0)] if "time" in request.url.path else [(901, 8.1), (902, 8.0)]
        assert request.url.path.startswith("/show/2--")
        return httpx.Response(200, text=_listing(entries))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await LibvioCatalog(
                client, recent_limit=1, year_span=1, current_year=2026, request_interval_seconds=0
            ).discover_series()

    result = asyncio.run(scenario())
    assert result.complete and {detail.public_media_id for detail in result.details} == {
        "900",
        "901",
    }
    assert all(
        [episode.source_index for episode in detail.episodes] == [0, 1] for detail in result.details
    )


def test_movie_with_multiple_quality_entries_is_not_a_series():
    html = _detail_html(1).replace(
        "</li></ul>", '</li><li><a href="/play/1-4-2.html">高清版</a></li></ul>'
    )
    assert (
        parse_libvio_detail(html, source_url="https://www.libvio.lat/detail/1.html").kind == "movie"
    )


def test_new_series_with_one_episode_and_unknown_total_is_a_series():
    html = (
        _detail_html(1, series=True, count=1)
        .replace("总集数：12", "总集数：0")
        .replace("海外剧", "剧情,日本")
    )
    detail = parse_libvio_detail(html, source_url="https://www.libvio.lat/detail/1.html")
    assert detail.kind == "series" and len(detail.episodes) == 1


def test_discovery_category_takes_precedence_over_ambiguous_episode_labels():
    html = _detail_html(1).replace("HD中字", "第1集")
    assert (
        parse_libvio_detail(
            html, source_url="https://www.libvio.lat/detail/1.html", kind_hint="movie"
        ).kind
        == "movie"
    )


@pytest.mark.parametrize(
    "bad_next",
    ["https://evil.example/show/1--time------2---.html", "/show/2--time------2---.html", "repeat"],
)
def test_bad_pagination_aborts_without_accepting_partial_snapshot(bad_next):
    def handle(request):
        next_url = "/show/1--time------2---.html" if bad_next == "repeat" else bad_next
        return httpx.Response(200, text=_listing([(1, 9)], next_url))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ResolverError):
                await LibvioCatalog(
                    client, recent_limit=3, year_span=0, request_interval_seconds=0
                ).discover_movies()

    asyncio.run(scenario())


def test_automatic_snapshot_preserves_policies_ids_names_and_other_provider(tmp_path):
    path = str(tmp_path / "library.db")
    repository = create_media_repository(path)
    xlys = CatalogEntry(
        xlys_id=1,
        title="影片1",
        year=2026,
        cover_url=None,
        source_updated_on="2026-10-01",
        dav_filename="影片1 (2026).strm",
    )
    repository.import_discovered_movies((xlys,))
    kept = repository.import_libvio_detail(_detail(1))
    hidden = repository.import_libvio_detail(_detail(2))
    repository.set_media_policy(hidden.xlys_id, MoviePolicy.HIDDEN)
    repository.import_discovered_libvio((_detail(3), _detail(4)), kind="movie")
    ids = {
        item.source_media_id: item.xlys_id
        for item in repository.list_media()
        if item.provider == "libvio"
    }
    assert kept.dav_name.endswith("[libvio-1].strm")
    assert repository.get_media(ids["3"]).policy == "auto"
    repository.import_discovered_libvio(
        (_detail(1), _detail(2), replace(_detail(3), title="新标题")), kind="movie"
    )
    assert repository.get_media(ids["4"]) is None
    assert repository.get_media(kept.xlys_id).policy == "keep"
    assert repository.get_media(hidden.xlys_id).policy == "hidden"
    assert repository.get_media(ids["3"]).dav_name == "影片3 (2026).strm"
    assert repository.get_media(1).provider == "xlys"
    repository.import_discovered_libvio((_detail(5),), kind="movie", replace_auto=False)
    assert repository.get_media(ids["3"]) is not None
    repository.import_discovered_libvio((), kind="movie")
    assert repository.get_media(ids["3"]) is not None
    repository.close()
    repository = create_media_repository(path)
    try:
        repository.import_discovered_libvio((_detail(1), _detail(2), _detail(3)), kind="movie")
        assert repository.get_media(ids["3"]).source_media_id == "3"
        assert repository.get_media(kept.xlys_id).dav_name == kept.dav_name
    finally:
        repository.close()


def test_series_snapshot_refreshes_episodes_and_preserves_recently_watched():
    repository = create_media_repository(":memory:")
    try:
        repository.import_discovered_libvio(
            tuple(_detail(source_id, series=True) for source_id in (1, 2, 3)), kind="series"
        )
        records = {item.source_media_id: item for item in repository.list_media()}
        repository.mark_series_watched("https://www.libvio.lat/play/2-4-2.html")
        with repository.engine.begin() as connection:
            connection.execute(
                text("UPDATE media_items SET last_watched_at=:date WHERE source_media_id='3'"),
                {"date": datetime.now(timezone.utc) - timedelta(days=31)},
            )
        repository.import_discovered_libvio((_detail(1, series=True, count=3),), kind="series")
        assert len(repository.list_episodes(records["1"].xlys_id)) == 3
        assert repository.get_media(records["2"].xlys_id) is not None
        assert repository.get_media(records["3"].xlys_id) is None
        assert repository.get_media(records["1"].xlys_id).dav_name == records["1"].dav_name
    finally:
        repository.close()


def test_libvio_batch_rolls_back_when_existing_kind_conflicts():
    repository = create_media_repository(":memory:")
    try:
        repository.import_libvio_detail(_detail(1, series=True))
        with pytest.raises(ValueError):
            repository.import_discovered_libvio((_detail(2), _detail(1)), kind="movie")
        assert len(repository.list_media()) == 1
    finally:
        repository.close()


class _XlysStub:
    recent_limit = 50
    origin = "https://www.xlys02.com"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def discover_movies(self):
        self.calls.append("movie")
        if self.fail:
            raise ResolverError("site unavailable")
        return ()

    async def discover_series(self):
        self.calls.append("series")
        return ()


class _LibvioStub:
    def __init__(self):
        self.calls = []

    async def discover_movies(self):
        self.calls.append("movie")
        return LibvioDiscovery((_detail(1),))

    async def discover_series(self):
        self.calls.append("series")
        return LibvioDiscovery((_detail(2, series=True),))


@pytest.mark.parametrize("xlys_fails", [False, True])
def test_admin_sync_adds_both_kinds_webdav_and_reports_partial_provider_failure(xlys_fails):
    application = create_app(AppSettings(database_path=":memory:", log_file=None))
    with TestClient(application, base_url="http://192.168.50.154:8787") as client:
        services = get_app_services(application)
        services.media_library.catalog = _XlysStub(xlys_fails)
        services.media_library.libvio_catalog = _LibvioStub()
        kept = services.media_repository.import_libvio_detail(_detail(9))
        response = client.post("/api/admin/sync", auth=("demo", "demo"))
        assert response.status_code == 200
        catalog = response.json()
        assert catalog["counts"]["automatic"] == 2 and catalog["counts"]["kept"] == 1
        assert len(catalog["discovery_failures"]) == (1 if xlys_fails else 0)
        assert services.media_repository.get_media(kept.xlys_id).policy == "keep"
        movie = next(item for item in catalog["movies"] if item["source_media_id"] == "1")
        result = client.get("/dav/电影/" + quote(movie["dav_filename"]), auth=("demo", "demo"))
        assert result.status_code == 200
        assert parse_qs(urlsplit(result.text.strip()).query)["page_url"] == [movie["play_page_url"]]
        series = next(item for item in catalog["movies"] if item["source_media_id"] == "2")
        assert series["available_episode_count"] == 2
        listing = client.request(
            "PROPFIND",
            "/dav/电视剧/" + quote(series["dav_filename"]) + "/Season 01/",
            auth=("demo", "demo"),
            headers={"Depth": "1"},
        )
        assert listing.status_code == 207 and "S01E02.strm" in listing.text


def test_incomplete_discovery_refreshes_successes_without_pruning_and_empty_bootstrap_works():
    class PartialCatalog(_LibvioStub):
        async def discover_movies(self):
            return LibvioDiscovery((_detail(1),), ("LIBVIO 详情获取失败",))

    repository = create_media_repository(":memory:")
    try:
        library = MediaLibrary(repository, _XlysStub(True), libvio_catalog=_LibvioStub())
        assert len(asyncio.run(library.visible_movies())) == 1
        repository.import_discovered_libvio((_detail(1), _detail(8)), kind="movie")
        library.libvio_catalog = PartialCatalog()
        asyncio.run(library.sync_movies_from_source())
        assert {item.source_media_id for item in repository.list_media()} == {"1", "8"}
        assert len(library.last_discovery_failures) == 2
    finally:
        repository.close()


def test_supplier_sync_is_authenticated_validated_and_only_calls_selected_provider():
    application = create_app(AppSettings(database_path=":memory:", log_file=None))
    with TestClient(application) as client:
        services = get_app_services(application)
        xlys = _XlysStub(True)
        libvio = _LibvioStub()
        services.media_library.catalog = xlys
        services.media_library.libvio_catalog = libvio
        services.media_repository.import_discovered_movies(
            (
                CatalogEntry(
                    xlys_id=10,
                    title="旧供应商影片",
                    year=2026,
                    cover_url=None,
                    source_updated_on="2026-10-01",
                    dav_filename="旧供应商影片.strm",
                ),
            )
        )
        auth = ("demo", "demo")
        assert client.post("/api/admin/sync?provider=libvio").status_code == 401
        assert client.post("/api/admin/sync?provider=unknown", auth=auth).status_code == 422
        assert not xlys.calls and not libvio.calls
        response = client.post("/api/admin/sync?provider=libvio", auth=auth)
        assert response.status_code == 200 and not xlys.calls
        assert set(libvio.calls) == {"movie", "series"}
        assert response.json()["discovery_failures"] == []
        assert services.media_repository.get_media(10).dav_name == "旧供应商影片.strm"
        statuses = {item["provider"]: item for item in response.json()["provider_syncs"]}
        assert statuses["libvio"]["status"] == "success"
        assert statuses["libvio"]["last_synced_at"] is not None
        assert statuses["xlys"]["status"] == "idle"


def test_failed_provider_retains_catalog_and_status_when_another_provider_syncs():
    class ClosedSite(_XlysStub):
        async def discover_series(self):
            self.calls.append("series")
            raise ResolverError("site unavailable")

    application = create_app(AppSettings(database_path=":memory:", log_file=None))
    with TestClient(application) as client:
        services = get_app_services(application)
        services.media_library.catalog = ClosedSite(True)
        libvio = _LibvioStub()
        services.media_library.libvio_catalog = libvio
        kept = services.media_repository.import_libvio_detail(_detail(9))
        auth = ("demo", "demo")
        failed = client.post("/api/admin/sync?provider=xlys", auth=auth)
        assert failed.status_code == 502 and not libvio.calls
        listing = client.get("/api/admin/media", auth=auth).json()
        assert listing["counts"]["total"] == 1
        states = {item["provider"]: item for item in listing["provider_syncs"]}
        assert states["xlys"]["status"] == "failed" and len(states["xlys"]["failures"]) == 2
        successful = client.post("/api/admin/sync?provider=libvio", auth=auth).json()
        assert successful["discovery_failures"] == []
        new_states = {item["provider"]: item for item in successful["provider_syncs"]}
        assert new_states["xlys"] == states["xlys"]
        assert new_states["libvio"]["status"] == "success"
        assert services.media_repository.get_media(kept.xlys_id).policy == "keep"


def test_provider_locks_allow_libvio_to_finish_while_xlys_is_waiting():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        class SlowSite(_XlysStub):
            async def discover_movies(self):
                entered.set()
                await release.wait()
                return ()

        repository = create_media_repository(":memory:")
        try:
            library = MediaLibrary(repository, SlowSite(), libvio_catalog=_LibvioStub())
            slow = asyncio.create_task(library.sync_from_source(provider="xlys"))
            await entered.wait()
            assert library.provider_sync_states["xlys"].status == "syncing"
            try:
                await asyncio.wait_for(library.sync_from_source(provider="libvio"), timeout=2)
                assert not slow.done()
                assert library.provider_sync_states["libvio"].status == "success"
            finally:
                release.set()
                await slow
        finally:
            repository.close()

    asyncio.run(scenario())


def test_partial_provider_sync_reports_detail_failures_and_preserves_previous_auto_titles():
    class Partial(_LibvioStub):
        async def discover_movies(self):
            return LibvioDiscovery((_detail(1),), ("详情获取失败",))

    repository = create_media_repository(":memory:")
    try:
        repository.import_discovered_libvio((_detail(1), _detail(8)), kind="movie")
        library = MediaLibrary(repository, _XlysStub(), libvio_catalog=Partial())
        asyncio.run(library.sync_from_source(provider="libvio"))
        assert library.provider_sync_states["libvio"].status == "partial"
        assert {item.source_media_id for item in repository.list_media()} == {"1", "8", "2"}
        assert library.provider_sync_states["xlys"].status == "idle"
    finally:
        repository.close()


def test_provider_sync_result_survives_service_restart(tmp_path):
    path = str(tmp_path / "sync-history.db")
    repository = create_media_repository(path)
    library = MediaLibrary(repository, _XlysStub(True), libvio_catalog=_LibvioStub())
    asyncio.run(library.sync_from_source())
    states = dict(library.provider_sync_states)
    assert states["xlys"].status == "partial" and states["libvio"].status == "success"
    repository.close()
    reopened = create_media_repository(path)
    try:
        restored = MediaLibrary(reopened, _XlysStub(), libvio_catalog=_LibvioStub())
        assert restored.provider_sync_states == states
        assert len(reopened.list_media()) == 2
    finally:
        reopened.close()
