import asyncio

import httpx

from strm_proxy.segment_cache import SegmentCache, SegmentPayload


SEGMENT_PACKET = b"\x47" + b"\x00" * 187


def _manifest(count: int, *, duration: float = 6.0) -> str:
    lines = ["#EXTM3U"]
    for index in range(count):
        lines.extend(
            (
                f"#EXTINF:{duration},",
                f"https://vod.xl01.me/{index}.ts",
            )
        )
    return "\n".join(lines) + "\n"


def test_cache_is_global_lru_bounded_by_bytes() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            cache = SegmentCache(
                client,
                segment_host="vod.xl01.me",
                max_bytes=8,
                prefetch_seconds=600,
            )
            first = cache.claim("https://vod.xl01.me/1.ts", "https://page/1")
            cache.complete_foreground(first, SegmentPayload(b"12345"))
            second = cache.claim("https://vod.xl01.me/2.ts", "https://page/2")
            cache.complete_foreground(second, SegmentPayload(b"67890"))

            assert cache.total_bytes == 5
            assert cache.cached(
                "https://vod.xl01.me/1.ts", "https://page/1"
            ) is None
            assert cache.cached(
                "https://vod.xl01.me/2.ts", "https://page/2"
            ) == SegmentPayload(b"67890")

    asyncio.run(scenario())


def test_concurrent_claims_share_one_segment_result() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            cache = SegmentCache(
                client,
                segment_host="vod.xl01.me",
                max_bytes=1024,
                prefetch_seconds=600,
            )
            owner = cache.claim("https://vod.xl01.me/1.ts", "https://page")
            joined = cache.claim("https://vod.xl01.me/1.ts", "https://page")

            assert owner.owner is True
            assert joined.waiter is not None
            cache.complete_foreground(owner, SegmentPayload(b"segment"))

            assert await joined.waiter == SegmentPayload(b"segment")
            assert cache.entry_count == 1

    asyncio.run(scenario())


def test_prefetch_uses_extinf_window_and_caches_clean_ts() -> None:
    async def scenario() -> None:
        requested: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            return httpx.Response(
                200,
                stream=httpx.ByteStream(
                    b"fake-prefix" + SEGMENT_PACKET * 4
                ),
                request=request,
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            cache = SegmentCache(
                client,
                segment_host="vod.xl01.me",
                max_bytes=1024 * 1024,
                prefetch_seconds=10,
            )
            playlist = cache.register_playlist(
                "https://www.xlys02.com/play/1-0.htm",
                0,
                _manifest(4),
            )
            assert playlist is not None

            cache.start_prefetch(
                playlist,
                0,
                requested_url="https://vod.xl01.me/0.ts",
            )
            await cache._prefetch_tasks[playlist]

            assert requested == ["/1.ts", "/2.ts"]
            first = cache.cached(
                "https://vod.xl01.me/1.ts",
                "https://www.xlys02.com/play/1-0.htm",
            )
            second = cache.cached(
                "https://vod.xl01.me/2.ts",
                "https://www.xlys02.com/play/1-0.htm",
            )
            assert first is not None
            assert second is not None
            assert first.data == SEGMENT_PACKET * 4
            assert second.data == SEGMENT_PACKET * 4

    asyncio.run(scenario())


def test_zero_capacity_disables_registration_and_caching() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            cache = SegmentCache(
                client,
                segment_host="vod.xl01.me",
                max_bytes=0,
                prefetch_seconds=600,
            )

            assert cache.enabled is False
            assert cache.register_playlist("https://page", 0, _manifest(2)) is None

    asyncio.run(scenario())
