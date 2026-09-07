import asyncio
import gzip

from strm_proxy.hls import (
    decode_wrapped_manifest,
    find_mpeg_ts_offset,
    first_playlist_uri,
    iter_aligned_mpeg_ts,
)


def test_decode_wrapped_manifest_rewrites_segments() -> None:
    source = "#EXTM3U\n#EXTINF:6,\nabc.ts\n#EXT-X-ENDLIST\n"
    wrapped = b"x" * 3354 + gzip.compress(source.encode())
    assert decode_wrapped_manifest(wrapped, "https://www.xlys02.com/a.m3u8#iplay") == (
        "#EXTM3U\n#EXTINF:6,\nhttps://vod.xl01.me/abc.ts\n#EXT-X-ENDLIST\n"
    )


def test_special_line_uses_hls_segment_directory() -> None:
    source = "#EXTM3U\nabc.ts\n"
    assert decode_wrapped_manifest(
        source.encode(),
        "https://www.xlys02.com/a.m3u8#ac5634-us",
    ) == "#EXTM3U\nhttps://vod.xl01.me/hls/abc.ts\n"


def test_finds_ts_inside_png_wrapper() -> None:
    packet = bytes([0x47]) + b"x" * 187
    wrapped = b"\x89PNG\r\n\x1a\n" + b"fake" * 20 + b"IEND" + b"\x00" * 8 + packet * 3
    assert find_mpeg_ts_offset(wrapped) == len(wrapped) - len(packet) * 3


def test_finds_first_variant_or_media_uri() -> None:
    assert first_playlist_uri(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nhttps://cdn/a.m3u8\n"
    ) == "https://cdn/a.m3u8"


def test_aligned_ts_stream_drops_trailing_wrapper_bytes() -> None:
    packet = b"\x47" + b"x" * 187

    async def scenario() -> bytes:
        async def remainder():
            yield packet[100:] + packet + b"image-trailer"

        chunks = [
            chunk
            async for chunk in iter_aligned_mpeg_ts(
                packet * 2 + packet[:100],
                remainder(),
                source_url="https://vod.xl01.me/example.ts",
            )
        ]
        return b"".join(chunks)

    assert asyncio.run(scenario()) == packet * 4


def test_aligned_ts_stream_resynchronizes_after_middle_garbage() -> None:
    packet = b"\x47" + b"x" * 187

    async def scenario() -> bytes:
        async def remainder():
            yield packet * 2

        chunks = [
            chunk
            async for chunk in iter_aligned_mpeg_ts(
                packet * 3 + b"junk" + packet,
                remainder(),
                source_url="https://vod.xl01.me/example.ts",
            )
        ]
        return b"".join(chunks)

    assert asyncio.run(scenario()) == packet * 6
