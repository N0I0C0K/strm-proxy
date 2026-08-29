import gzip

from strm_proxy.hls import decode_wrapped_manifest, find_mpeg_ts_offset


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
