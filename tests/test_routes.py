import asyncio
import logging
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from strm_proxy.app import create_app
from strm_proxy.config import AppSettings
from strm_proxy.dependencies import get_app_services, get_http_client, get_resolver
from strm_proxy.models import ResolvedPage, ResolverError, StreamCandidate


PAGE_URL = "https://www.xlys02.com/play/27062-0.htm"


class _StubResolver:
    member_access_enabled = False
    allowed_hosts = ("www.xlys02.com", "xlys02.com")

    def __init__(
        self,
        *,
        tos_available: bool = False,
        member_available: bool = False,
        direct_url: str | None = None,
        healthy_hls_line: int = 0,
    ) -> None:
        self.tos_available = tos_available
        self.member_available = member_available
        self.direct_url = direct_url
        self.healthy_hls_line = healthy_hls_line
        self.direct_source_calls: list[str] = []
        self.fetch_manifest_lines: list[int] = []
        self.resolve_page_calls = 0
        self.working_hls_line_calls = 0

    async def resolve_page(self, page_url: str) -> ResolvedPage:
        self.resolve_page_calls += 1
        return ResolvedPage(
            page_url=page_url,
            pid=204486,
            title="痴迷",
            candidates=(
                StreamCandidate(
                    kind="m3u8",
                    url="https://www.xlys02.com/wrapped.m3u8#inews",
                ),
                StreamCandidate(
                    kind="m3u8_2",
                    url="https://www.xlys02.com/wrapped-2.m3u8#iplay",
                ),
            ),
            tos_available=self.tos_available,
            member_token="available" if self.member_available else None,
        )

    async def resolve_direct_media(self, page_url: str, source: str):
        self.direct_source_calls.append(source)
        if self.direct_url is None:
            raise ResolverError(f"{source} unavailable")
        return await self.resolve_page(page_url), self.direct_url

    async def find_working_hls_line(
        self,
        page_url: str,
        preferred_line: int = 0,
    ) -> int:
        self.working_hls_line_calls += 1
        return self.healthy_hls_line

    async def fetch_manifest(
        self,
        page_url: str,
        line: int = 0,
        segment_url_builder=None,
    ):
        self.fetch_manifest_lines.append(line)
        segment_url = "https://vod.xl01.me/abc.ts"
        if segment_url_builder is not None:
            segment_url = segment_url_builder(segment_url)
        return None, None, f"#EXTM3U\n#EXTINF:6,\n{segment_url}\n"


@pytest.mark.parametrize(
    ("proxy_segments", "expected"),
    [
        (True, False),
        (False, True),
    ],
)
def test_raw_segment_preference_is_derived_from_proxy_mode(
    proxy_segments: bool,
    expected: bool,
) -> None:
    application = create_app(
        AppSettings(
            database_path=":memory:",
            proxy_segments=proxy_segments,
        )
    )

    with TestClient(application):
        services = get_app_services(application)
        resolver = services.resolver
        assert resolver.prefer_raw_segments is expected
        assert services.segment_cache.enabled is proxy_segments


@pytest.mark.parametrize(
    ("configured", "query_override", "expects_proxy"),
    [
        (True, "false", True),
        (False, "true", False),
    ],
)
def test_hls_uses_global_proxy_setting_and_ignores_query_override(
    configured: bool,
    query_override: str,
    expects_proxy: bool,
) -> None:
    application = create_app(
        AppSettings(database_path=":memory:", proxy_segments=configured)
    )
    application.dependency_overrides[get_resolver] = lambda: _StubResolver()

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        response = client.get(
            "/hls.m3u8",
            params={
                "page_url": PAGE_URL,
                "proxy_segments": query_override,
            },
        )

    assert response.status_code == 200
    assert ("/segment?" in response.text) is expects_proxy
    assert ("playlist=" in response.text) is expects_proxy
    assert ("index=0" in response.text) is expects_proxy


def test_zero_segment_cache_capacity_keeps_proxy_without_prefetch_metadata() -> None:
    application = create_app(
        AppSettings(database_path=":memory:", segment_cache_max_mb=0)
    )
    application.dependency_overrides[get_resolver] = lambda: _StubResolver()

    with TestClient(application) as client:
        response = client.get("/hls.m3u8", params={"page_url": PAGE_URL})

    assert response.status_code == 200
    assert "/segment?" in response.text
    assert "playlist=" not in response.text
    assert "index=" not in response.text


def test_strm_url_is_stable_and_omits_proxy_setting() -> None:
    application = create_app(
        AppSettings(database_path=":memory:", proxy_segments=False)
    )

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        response = client.get(
            "/strm",
            params={
                "page_url": PAGE_URL,
                "proxy_segments": "true",
            },
        )

    assert response.status_code == 200
    assert "proxy_segments" not in response.text
    assert response.text.startswith("http://192.168.1.20:8787/play?")
    assert "source=" not in response.text
    assert "line=" not in response.text


def test_segment_response_is_cached_after_first_stream() -> None:
    packet = b"\x47" + b"\x00" * 187
    upstream_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(
            200,
            headers={"Cache-Control": "public, max-age=60"},
            stream=httpx.ByteStream(b"fake-prefix" + packet * 4),
            request=request,
        )

    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    application = create_app(AppSettings(database_path=":memory:"))
    application.dependency_overrides[get_http_client] = lambda: upstream_client
    params = {
        "url": "https://vod.xl01.me/segment.ts",
        "referer": PAGE_URL,
    }

    try:
        with TestClient(application) as client:
            first = client.get("/segment", params=params)
            second = client.get("/segment", params=params)
            head = client.head("/segment", params=params)
    finally:
        asyncio.run(upstream_client.aclose())

    assert first.status_code == 200
    assert first.content == packet * 4
    assert second.content == packet * 4
    assert second.headers["content-length"] == str(len(packet) * 4)
    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(packet) * 4)
    assert upstream_calls == 1


@pytest.mark.parametrize(
    "segment_url",
    [
        "http://vod.xl01.me/video.ts",
        "https://evil.example/video.ts",
        "https://user:password@vod.xl01.me/video.ts",
        "https://vod.xl01.me:444/video.ts",
        "https://vod.xl01.me:invalid/video.ts",
    ],
)
def test_segment_rejects_untrusted_urls(segment_url: str) -> None:
    application = create_app(AppSettings(database_path=":memory:"))

    with TestClient(application) as client:
        response = client.get("/segment", params={"url": segment_url})

    assert response.status_code == 400


def test_segment_upstream_error_response_redacts_query_parameters() -> None:
    secret = "must-not-leak"

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    application = create_app(AppSettings(database_path=":memory:"))
    application.dependency_overrides[get_http_client] = lambda: upstream_client

    try:
        with TestClient(application) as client:
            response = client.get(
                "/segment",
                params={
                    "url": (
                        "https://vod.xl01.me/video.ts"
                        f"?token={secret}"
                    ),
                    "referer": PAGE_URL,
                },
            )
    finally:
        asyncio.run(upstream_client.aclose())

    assert response.status_code == 502
    assert secret not in response.text
    assert response.json()["detail"] == (
        "Upstream request failed: ConnectError for "
        "https://vod.xl01.me/video.ts"
    )


def test_strm_can_explicitly_pin_a_debug_source() -> None:
    application = create_app(AppSettings(database_path=":memory:"))

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        response = client.get(
            "/strm",
            params={"page_url": PAGE_URL, "source": "tos"},
        )

    assert "source=tos" in response.text


def test_play_auto_redirects_to_verified_tos_media() -> None:
    application = create_app(AppSettings(database_path=":memory:"))
    application.dependency_overrides[get_resolver] = lambda: _StubResolver(
        tos_available=True,
        direct_url="https://p16-hera-sign-va.ibyteimg.com/obj/video",
    )

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        response = client.get(
            "/play",
            params={"page_url": PAGE_URL},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://p16-hera-sign-va")
    assert response.headers["cache-control"] == "no-store"


def test_play_logs_decision_without_media_query_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    application = create_app(AppSettings(database_path=":memory:"))
    application.dependency_overrides[get_resolver] = lambda: _StubResolver(
        tos_available=True,
        direct_url=(
            "https://p16-hera-sign-va.ibyteimg.com/obj/video"
            "?token=must-not-be-logged"
        ),
    )
    caplog.set_level(logging.INFO, logger="strm_proxy.routes")

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        response = client.get(
            "/play",
            params={"page_url": PAGE_URL},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert "event=play_selected" in caplog.text
    assert "mode=direct" in caplog.text
    assert "must-not-be-logged" not in caplog.text


def test_play_auto_falls_back_to_local_hls() -> None:
    application = create_app(AppSettings(database_path=":memory:"))
    application.dependency_overrides[get_resolver] = lambda: _StubResolver()

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        response = client.get(
            "/play",
            params={"page_url": PAGE_URL},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["location"].startswith(
        "http://192.168.1.20:8787/hls.m3u8?"
    )
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["v"][0]


def test_play_auto_explores_hls_when_direct_source_fails() -> None:
    application = create_app(AppSettings(database_path=":memory:"))
    resolver = _StubResolver(healthy_hls_line=1)
    resolver.tos_available = True
    application.dependency_overrides[get_resolver] = lambda: resolver

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        response = client.get(
            "/play",
            params={"page_url": PAGE_URL},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert resolver.resolve_page_calls == 1
    assert resolver.direct_source_calls == ["tos"]
    assert resolver.working_hls_line_calls == 1
    assert "line=" not in response.headers["location"]


def test_play_preserves_tos_then_member_source_priority() -> None:
    class PriorityResolver(_StubResolver):
        def __init__(self) -> None:
            super().__init__(tos_available=True, member_available=True)

        async def resolve_direct_media(self, page_url: str, source: str):
            self.direct_source_calls.append(source)
            if source == "tos":
                raise ResolverError("tos unavailable")
            return (
                await self.resolve_page(page_url),
                "https://member.example/video.mp4",
            )

    application = create_app(AppSettings(database_path=":memory:"))
    resolver = PriorityResolver()
    application.dependency_overrides[get_resolver] = lambda: resolver

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        response = client.get(
            "/play",
            params={"page_url": PAGE_URL},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["location"] == "https://member.example/video.mp4"
    assert resolver.direct_source_calls == ["tos", "member"]
    assert resolver.working_hls_line_calls == 0


def test_play_falls_back_to_first_healthy_hls_line() -> None:
    application = create_app(AppSettings(database_path=":memory:"))
    resolver = _StubResolver(
        tos_available=True,
        healthy_hls_line=1,
    )
    application.dependency_overrides[get_resolver] = lambda: resolver

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        response = client.get(
            "/play",
            params={"page_url": PAGE_URL},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert resolver.direct_source_calls == ["tos"]
    assert resolver.working_hls_line_calls == 1
    assert "line=" not in response.headers["location"]


def test_play_reuses_cached_hls_direction_before_other_sources() -> None:
    application = create_app(AppSettings(database_path=":memory:"))
    resolver = _StubResolver(
        tos_available=True,
        member_available=True,
        healthy_hls_line=1,
    )
    application.dependency_overrides[get_resolver] = lambda: resolver

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        first = client.get(
            "/play",
            params={"page_url": PAGE_URL},
            follow_redirects=False,
        )
        second = client.get(
            "/play",
            params={"page_url": PAGE_URL},
            follow_redirects=False,
        )

    assert first.status_code == 302
    assert second.status_code == 302
    assert "line=" not in second.headers["location"]
    assert resolver.direct_source_calls == ["tos", "member"]
    assert resolver.working_hls_line_calls == 1


def test_play_reuses_cached_member_direction_before_tos() -> None:
    class MemberResolver(_StubResolver):
        def __init__(self) -> None:
            super().__init__(tos_available=True, member_available=True)

        async def resolve_direct_media(self, page_url: str, source: str):
            self.direct_source_calls.append(source)
            if source == "tos":
                raise ResolverError("tos unavailable")
            return (
                await self.resolve_page(page_url),
                "https://member.example/video.mp4",
            )

    application = create_app(AppSettings(database_path=":memory:"))
    resolver = MemberResolver()
    application.dependency_overrides[get_resolver] = lambda: resolver

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        for _ in range(2):
            response = client.get(
                "/play",
                params={"page_url": PAGE_URL},
                follow_redirects=False,
            )
            assert response.status_code == 302

    assert resolver.direct_source_calls == ["tos", "member", "member"]
    assert resolver.working_hls_line_calls == 0


def test_failed_cached_direction_is_invalidated_before_full_fallback() -> None:
    class ChangingResolver(_StubResolver):
        def __init__(self) -> None:
            super().__init__(tos_available=True, member_available=True)
            self.tos_works = False
            self.member_works = True

        async def resolve_direct_media(self, page_url: str, source: str):
            self.direct_source_calls.append(source)
            if source == "tos" and self.tos_works:
                return await self.resolve_page(page_url), "https://tos.example/a.mp4"
            if source == "member" and self.member_works:
                return (
                    await self.resolve_page(page_url),
                    "https://member.example/a.mp4",
                )
            raise ResolverError(f"{source} unavailable")

    application = create_app(AppSettings(database_path=":memory:"))
    resolver = ChangingResolver()
    application.dependency_overrides[get_resolver] = lambda: resolver

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        first = client.get(
            "/play",
            params={"page_url": PAGE_URL},
            follow_redirects=False,
        )
        resolver.tos_works = True
        resolver.member_works = False
        second = client.get(
            "/play",
            params={"page_url": PAGE_URL},
            follow_redirects=False,
        )

    assert first.headers["location"] == "https://member.example/a.mp4"
    assert second.headers["location"] == "https://tos.example/a.mp4"
    assert resolver.direct_source_calls == ["tos", "member", "member", "tos"]


def test_play_selection_cache_can_be_disabled() -> None:
    application = create_app(
        AppSettings(
            database_path=":memory:",
            play_selection_cache=False,
        )
    )
    resolver = _StubResolver(tos_available=True, healthy_hls_line=1)
    application.dependency_overrides[get_resolver] = lambda: resolver

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        for _ in range(2):
            response = client.get(
                "/play",
                params={"page_url": PAGE_URL},
                follow_redirects=False,
            )
            assert response.status_code == 302

    assert resolver.direct_source_calls == ["tos", "tos"]
    assert resolver.working_hls_line_calls == 2


def test_play_failure_returns_retryable_503_and_is_cached() -> None:
    class FailingResolver(_StubResolver):
        async def find_working_hls_line(
            self,
            page_url: str,
            preferred_line: int = 0,
        ) -> int:
            self.working_hls_line_calls += 1
            raise ResolverError("all HLS lines failed")

    application = create_app(AppSettings(database_path=":memory:"))
    resolver = FailingResolver()
    application.dependency_overrides[get_resolver] = lambda: resolver

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        first = client.get("/play", params={"page_url": PAGE_URL})
        second = client.get("/play", params={"page_url": PAGE_URL})

    assert first.status_code == 503
    assert first.headers["retry-after"] == "30"
    assert first.headers["x-strm-proxy-error"] == "no-playable-source"
    assert first.json()["detail"] == {
        "code": "no_playable_source",
        "message": "All advertised playback sources failed",
        "errors": ["hls: all HLS lines failed"],
    }
    assert second.status_code == 503
    assert resolver.resolve_page_calls == 1
    assert resolver.working_hls_line_calls == 1


def test_play_explicit_hls_ignores_legacy_line_and_selects_server_side() -> None:
    application = create_app(AppSettings(database_path=":memory:"))
    resolver = _StubResolver(
        healthy_hls_line=1,
    )
    application.dependency_overrides[get_resolver] = lambda: resolver

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        response = client.get(
            "/play",
            params={"page_url": PAGE_URL, "source": "hls", "line": 0},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert "line=" not in response.headers["location"]
    assert resolver.working_hls_line_calls == 1


def test_hls_ignores_legacy_line_and_remaps_cached_route_name() -> None:
    application = create_app(AppSettings(database_path=":memory:"))
    resolver = _StubResolver(healthy_hls_line=0)
    application.dependency_overrides[get_resolver] = lambda: resolver

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        resolved = asyncio.run(resolver.resolve_page(PAGE_URL))
        get_app_services(application).playback.cache.remember_hls(
            resolved,
            1,
            manual_override=True,
        )
        response = client.get(
            "/hls.m3u8",
            params={"page_url": PAGE_URL, "line": 0},
        )

    assert response.status_code == 200
    assert resolver.fetch_manifest_lines == [1]
    assert resolver.working_hls_line_calls == 0


def test_hls_redirects_old_revision_to_current_before_fetching_manifest() -> None:
    application = create_app(AppSettings(database_path=":memory:"))
    resolver = _StubResolver(healthy_hls_line=0)
    application.dependency_overrides[get_resolver] = lambda: resolver

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        resolved = asyncio.run(resolver.resolve_page(PAGE_URL))
        selection = get_app_services(
            application
        ).playback.cache.remember_hls(resolved, 1, manual_override=True)
        response = client.get(
            "/hls.m3u8",
            params={"page_url": PAGE_URL, "v": "stale"},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["cache-control"] == "no-store"
        assert resolver.fetch_manifest_lines == []
        query = parse_qs(urlparse(response.headers["location"]).query)
        assert query["v"] == [selection.revision]

        current = client.get(response.headers["location"])

    assert current.status_code == 200
    assert resolver.fetch_manifest_lines == [1]


def test_hls_without_revision_redirects_to_current_revision() -> None:
    application = create_app(AppSettings(database_path=":memory:"))
    resolver = _StubResolver(healthy_hls_line=1)
    application.dependency_overrides[get_resolver] = lambda: resolver

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        response = client.get(
            "/hls.m3u8",
            params={"page_url": PAGE_URL},
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert resolver.fetch_manifest_lines == []
        location = response.headers["location"]
        assert parse_qs(urlparse(location).query)["v"][0]

        current = client.get(location)

    assert current.status_code == 200
    assert resolver.fetch_manifest_lines == [1]
    assert resolver.working_hls_line_calls == 1


def test_hls_revision_is_stable_when_selection_cache_is_disabled() -> None:
    application = create_app(
        AppSettings(database_path=":memory:", play_selection_cache=False)
    )
    resolver = _StubResolver(healthy_hls_line=1)
    application.dependency_overrides[get_resolver] = lambda: resolver

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        old = client.get(
            "/hls.m3u8",
            params={"page_url": PAGE_URL},
            follow_redirects=False,
        )
        current = client.get(old.headers["location"], follow_redirects=False)

    assert old.status_code == 302
    assert current.status_code == 200
    assert resolver.fetch_manifest_lines == [1]
    assert resolver.working_hls_line_calls == 2


def test_stale_segment_redirects_to_current_playlist_and_revision() -> None:
    application = create_app(AppSettings(database_path=":memory:"))
    resolver = _StubResolver(healthy_hls_line=1)
    application.dependency_overrides[get_resolver] = lambda: resolver

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        play = client.get(
            "/play",
            params={"page_url": PAGE_URL},
            follow_redirects=False,
        )
        manifest = client.get(play.headers["location"])
        current_segment_url = next(
            line for line in manifest.text.splitlines() if not line.startswith("#")
        )
        current_query = parse_qs(urlparse(current_segment_url).query)

        stale = client.get(
            "/segment",
            params={
                "url": "https://vod.xl01.me/old.ts",
                "referer": PAGE_URL,
                "playlist": "old-playlist",
                "index": 0,
                "v": "old-revision",
            },
            follow_redirects=False,
        )

    redirected_query = parse_qs(urlparse(stale.headers["location"]).query)
    assert stale.status_code == 302
    assert stale.headers["cache-control"] == "no-store"
    assert redirected_query["url"] == current_query["url"]
    assert redirected_query["playlist"] == current_query["playlist"]
    assert redirected_query["index"] == ["0"]
    assert redirected_query["v"] == current_query["v"]
