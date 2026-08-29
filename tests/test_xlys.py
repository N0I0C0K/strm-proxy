import asyncio
import gzip
from collections import Counter

import httpx
import pytest

from strm_proxy.models import ResolverError
from strm_proxy.xlys import (
    XlysResolver,
    create_signature,
    extract_candidates,
    parse_page,
    validate_page_url,
)


PAGE_URL = "https://www.xlys02.com/play/27062-0.htm"


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


def test_rejects_non_target_urls() -> None:
    with pytest.raises(ResolverError):
        validate_page_url(PAGE_URL.replace("www.xlys02.com", "example.com"), ("www.xlys02.com",))


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
