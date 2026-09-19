import asyncio
import gzip
from collections import Counter
from urllib.parse import parse_qs

import httpx
import pytest

from strm_proxy.models import ResolvedPage, ResolverError, StreamCandidate
from strm_proxy.xlys import (
    XlysResolver,
    create_signature,
    direct_media_url_candidates,
    extract_candidates,
    extract_direct_candidates,
    parse_page,
    validate_page_url,
)


PAGE_URL = "https://www.xlys02.com/play/27062-0.htm"


def _mpeg_ts_payload(prefix: bytes = b"") -> bytes:
    packet = b"\x47" + b"\x00" * 187
    return prefix + packet * 3


def test_signature_is_stable() -> None:
    assert create_signature(204486, 1787653355247) == (
        "8709DD7BCD094721016CD52A6D8D7840ECB64C97668A7C398194025296513B14"
    )


def test_parse_page() -> None:
    pid, title = parse_page(
        'var pid = 204486; var vod_name ="痴迷", vod_url ="/kehuan/27062.htm";',
        PAGE_URL,
    )
    assert pid == 204486
    assert title == "痴迷"


def test_extract_candidates_prefers_m3u8_fields_and_deduplicates() -> None:
    candidates = extract_candidates(
        {
            "m3u8": "https://www.xlys02.com/a.m3u8#iplay",
            "m3u8_2": "https://www.xlys02.com/a.m3u8#iplay,/b.m3u8",
            "url3": "https://cdn.example/video.mp4",
        },
        "https://www.xlys02.com",
    )
    assert [(item.kind, item.url) for item in candidates] == [
        ("m3u8", "https://www.xlys02.com/a.m3u8#iplay"),
        ("m3u8_2", "https://www.xlys02.com/b.m3u8"),
    ]
    assert candidates[0].route_name == "iplay"
    assert candidates[1].route_name is None


def test_extract_candidates_includes_additional_numbered_hls_fields() -> None:
    candidates = extract_candidates(
        {
            "m3u8": "https://cdn.example/one.m3u8#one",
            "url4": "https://cdn.example/four.m3u8#four",
            "m3u8_3": "https://cdn.example/three.m3u8#three",
            "url5": "https://cdn.example/video.mp4",
            "other": "https://cdn.example/unrelated.m3u8#other",
        },
        "https://www.xlys02.com",
    )
    assert [(item.kind, item.route_name) for item in candidates] == [
        ("m3u8", "one"),
        ("url4", "four"),
        ("m3u8_3", "three"),
    ]


def test_extract_direct_candidates_keeps_signed_url3_slots() -> None:
    candidates = extract_direct_candidates(
        {"url3": "https://cdn.example/video?x=1,https://other.example/video?x=2"},
        "https://www.xlys02.com",
    )
    assert [candidate.url for candidate in candidates] == [
        "https://cdn.example/video?x=1",
        "https://other.example/video?x=2",
    ]


def test_direct_media_candidates_rewrite_tos_objects_to_playable_cdn() -> None:
    candidates = direct_media_url_candidates(
        "https://p16-ulike-sg.ibyteimg.com/obj/"
        "tos-alisg-v-0000/example-object"
    )

    assert candidates[0] == (
        "https://p16-hera-sign-va.ibyteimg.com/obj/"
        "tos-alisg-v-0000/example-object"
    )


def test_rejects_non_target_urls() -> None:
    with pytest.raises(ResolverError):
        validate_page_url(PAGE_URL.replace("www.xlys02.com", "example.com"), ("www.xlys02.com",))


def test_rejects_play_url_with_invalid_port() -> None:
    with pytest.raises(ResolverError, match="URL is invalid"):
        validate_page_url(
            "https://www.xlys02.com:invalid/play/27062-0.htm",
            ("www.xlys02.com",),
        )


def test_accepts_legacy_category_play_url() -> None:
    url = "https://www.xlys02.com/guoju/play/24358-0.htm"

    assert validate_page_url(url, ("www.xlys02.com",)) == url


def test_resolver_caches_page_and_manifest() -> None:
    calls: Counter[str] = Counter()
    manifest = "#EXTM3U\n#EXTINF:6,\nabc.ts\n#EXT-X-ENDLIST\n"
    wrapped = b"x" * 3354 + gzip.compress(manifest.encode())

    def handle(request: httpx.Request) -> httpx.Response:
        calls[request.url.path] += 1
        if request.url.path == "/play/27062-0.htm":
            return httpx.Response(
                200,
                text='var pid = 204486; var vod_name ="\\u75F4\\u8FF7";',
            )
        if request.url.path == "/lines":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "m3u8": "https://www.xlys02.com/wrapped.m3u8#iplay"
                    },
                },
            )
        if request.url.path == "/wrapped.m3u8":
            return httpx.Response(200, content=wrapped)
        return httpx.Response(404)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
        ) as client:
            resolver = XlysResolver(client, cache_ttl_seconds=300)
            first = await resolver.fetch_manifest(PAGE_URL)
            second = await resolver.fetch_manifest(
                PAGE_URL,
                segment_url_builder=lambda url: f"http://proxy/segment?url={url}",
            )
            assert first[0].title == "痴迷"
            assert "https://vod.xl01.me/abc.ts" in first[2]
            assert "http://proxy/segment?url=https://vod.xl01.me/abc.ts" in second[2]

    asyncio.run(scenario())
    assert calls == Counter(
        {
            "/play/27062-0.htm": 1,
            "/lines": 1,
            "/wrapped.m3u8": 1,
        }
    )


def test_resolver_discovers_and_probes_tos_direct_media() -> None:
    calls: Counter[str] = Counter()

    def handle(request: httpx.Request) -> httpx.Response:
        calls[request.url.path] += 1
        if request.url.path == "/play/27062-0.htm":
            return httpx.Response(
                200,
                text='var pid = 204486; var vod_name ="痴迷";',
            )
        if request.url.path == "/lines":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"tos": "1", "ptoken": "888"},
                },
            )
        if request.url.path == "/god/204486":
            assert request.url.query == b"type=1"
            form = parse_qs(request.content.decode())
            assert form["verifyCode"] == ["888"]
            return httpx.Response(
                200,
                json={
                    "url": (
                        "https://p16-ulike-sg.ibyteimg.com/obj/"
                        "tos-alisg-v-0000/example-object"
                    )
                },
            )
        if request.url.host == "p16-hera-sign-va.ibyteimg.com":
            assert request.headers["range"] == "bytes=0-63"
            return httpx.Response(
                206,
                headers={"Content-Type": "video/mp4"},
                content=b"\x00\x00\x00\x20ftypisom" + b"x" * 52,
            )
        return httpx.Response(404)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
        ) as client:
            resolver = XlysResolver(client)
            resolved, media_url = await resolver.resolve_direct_media(
                PAGE_URL,
                "tos",
            )

        assert resolved.tos_available is True
        assert resolved.member_token == "888"
        assert media_url.startswith(
            "https://p16-hera-sign-va.ibyteimg.com/obj/"
        )

    asyncio.run(scenario())
    assert calls["/god/204486"] == 1


def test_member_source_requires_configured_login() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/play/27062-0.htm":
            return httpx.Response(200, text="var pid = 204486;")
        if request.url.path == "/lines":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"ptoken": "888"}},
            )
        return httpx.Response(404)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
        ) as client:
            resolver = XlysResolver(client, member_access_enabled=False)
            with pytest.raises(ResolverError, match="requires xlys login"):
                await resolver.resolve_direct_media(PAGE_URL, "member")

    asyncio.run(scenario())

def test_member_source_requires_automatic_captcha_recognition() -> None:
    god_called = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal god_called
        if request.url.path == "/play/27062-0.htm":
            return httpx.Response(200, text="var pid = 204486;")
        if request.url.path == "/lines":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"ptoken": "not-a-captcha"}},
            )
        if request.url.path == "/god/204486":
            god_called = True
        return httpx.Response(404)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
        ) as client:
            resolver = XlysResolver(client, member_access_enabled=True)
            with pytest.raises(ResolverError, match="automatic CAPTCHA"):
                await resolver.resolve_direct_media(PAGE_URL, "member")

    asyncio.run(scenario())
    assert god_called is False


def test_resolver_counts_dynamic_lines_and_skips_unhealthy_hls() -> None:
    wrapped_bad = b"x" * 3354 + gzip.compress(
        b"#EXTM3U\n#EXTINF:6,\nbad.ts\n#EXT-X-ENDLIST\n"
    )
    wrapped_good = b"x" * 3354 + gzip.compress(
        b"#EXTM3U\n#EXTINF:6,\ngood.ts\n#EXT-X-ENDLIST\n"
    )
    probed_segments: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/play/27062-0.htm":
            return httpx.Response(200, text="var pid = 204486;")
        if request.url.path == "/lines":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "m3u8": (
                            "https://www.xlys02.com/bad.m3u8,"
                            "https://www.xlys02.com/good.m3u8"
                        )
                    },
                },
            )
        if request.url.path == "/bad.m3u8":
            return httpx.Response(200, content=wrapped_bad)
        if request.url.path == "/good.m3u8":
            return httpx.Response(200, content=wrapped_good)
        if request.url.host == "vod.xl01.me":
            probed_segments.append(request.url.path)
            if request.url.path == "/bad.ts":
                return httpx.Response(403)
            return httpx.Response(
                206,
                headers={"Content-Type": "image/png"},
                content=_mpeg_ts_payload(b"\x89PNG\r\n\x1a\n"),
            )
        return httpx.Response(404)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
        ) as client:
            resolver = XlysResolver(client)
            resolved = await resolver.resolve_page(PAGE_URL)
            selected = await resolver.find_working_hls_line(PAGE_URL)

        assert len(resolved.candidates) == 2
        assert selected == 1

    asyncio.run(scenario())
    assert probed_segments == ["/bad.ts", "/good.ts"]


@pytest.mark.parametrize(
    ("prefer_raw_segments", "expected_line", "expected_probes"),
    [
        (True, 1, ["/wrapped.ts", "/raw.ts"]),
        (False, 0, ["/wrapped.ts", "/raw.ts"]),
    ],
)
def test_resolver_can_prefer_raw_hls_segments(
    prefer_raw_segments: bool,
    expected_line: int,
    expected_probes: list[str],
) -> None:
    manifests = {
        "/wrapped.m3u8": b"#EXTM3U\n#EXTINF:6,\nwrapped.ts\n#EXT-X-ENDLIST\n",
        "/raw.m3u8": b"#EXTM3U\n#EXTINF:6,\nraw.ts\n#EXT-X-ENDLIST\n",
    }
    probed_segments: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/play/27062-0.htm":
            return httpx.Response(200, text="var pid = 204486;")
        if request.url.path == "/lines":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "m3u8": (
                            "https://www.xlys02.com/wrapped.m3u8,"
                            "https://www.xlys02.com/raw.m3u8"
                        )
                    },
                },
            )
        if request.url.path in manifests:
            return httpx.Response(200, content=manifests[request.url.path])
        if request.url.host == "vod.xl01.me":
            probed_segments.append(request.url.path)
            if request.url.path == "/wrapped.ts":
                return httpx.Response(
                    206,
                    headers={"Content-Type": "image/png"},
                    content=_mpeg_ts_payload(b"\x89PNG\r\n\x1a\n"),
                )
            return httpx.Response(
                206,
                headers={"Content-Type": "application/octet-stream"},
                content=_mpeg_ts_payload(),
            )
        return httpx.Response(404)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
        ) as client:
            resolver = XlysResolver(
                client,
                prefer_raw_segments=prefer_raw_segments,
            )
            selected = await resolver.find_working_hls_line(PAGE_URL)

        assert selected == expected_line

    asyncio.run(scenario())
    assert probed_segments == expected_probes


def test_resolver_falls_back_to_first_wrapped_hls_line() -> None:
    manifest = b"#EXTM3U\n#EXTINF:6,\n{segment}.ts\n#EXT-X-ENDLIST\n"
    probed_segments: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/play/27062-0.htm":
            return httpx.Response(200, text="var pid = 204486;")
        if request.url.path == "/lines":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "m3u8": (
                            "https://www.xlys02.com/first.m3u8,"
                            "https://www.xlys02.com/second.m3u8"
                        )
                    },
                },
            )
        if request.url.path == "/first.m3u8":
            return httpx.Response(
                200,
                content=manifest.replace(b"{segment}", b"first"),
            )
        if request.url.path == "/second.m3u8":
            return httpx.Response(
                200,
                content=manifest.replace(b"{segment}", b"second"),
            )
        if request.url.host == "vod.xl01.me":
            probed_segments.append(request.url.path)
            return httpx.Response(
                206,
                headers={"Content-Type": "image/png"},
                content=_mpeg_ts_payload(b"\x89PNG\r\n\x1a\n"),
            )
        return httpx.Response(404)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
        ) as client:
            resolver = XlysResolver(client, prefer_raw_segments=True)
            selected = await resolver.find_working_hls_line(PAGE_URL)

        assert selected == 0

    asyncio.run(scenario())
    assert probed_segments == ["/first.ts", "/second.ts"]


def test_hls_probe_streams_until_it_finds_a_deep_wrapper() -> None:
    request_count = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        assert "range" not in request.headers
        return httpx.Response(
            200,
            content=_mpeg_ts_payload(b"x" * 5000),
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
        ) as client:
            resolver = XlysResolver(client)
            media_kind = await resolver._probe_hls_media(
                "https://vod.xl01.me/deep-wrapper.ts",
                PAGE_URL,
            )

        assert media_kind == "wrapped"

    asyncio.run(scenario())
    assert request_count == 1


def test_parallel_hls_probe_does_not_wait_for_slow_preferred_line() -> None:
    class SlowFirstLineResolver(XlysResolver):
        first_line_cancelled = False

        async def resolve_page(self, page_url: str) -> ResolvedPage:
            return ResolvedPage(
                page_url=page_url,
                pid=204486,
                title="痴迷",
                candidates=(
                    StreamCandidate(kind="m3u8", url="https://example/0.m3u8"),
                    StreamCandidate(kind="m3u8", url="https://example/1.m3u8"),
                ),
            )

        async def _probe_hls_line(self, page_url: str, line: int):
            if line == 0:
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    self.first_line_cancelled = True
                    raise
            return "raw"

    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            resolver = SlowFirstLineResolver(
                client,
                prefer_raw_segments=False,
            )
            selected = await asyncio.wait_for(
                resolver.find_working_hls_line(PAGE_URL),
                timeout=1,
            )

        assert selected == 1
        assert resolver.first_line_cancelled is True

    asyncio.run(scenario())
