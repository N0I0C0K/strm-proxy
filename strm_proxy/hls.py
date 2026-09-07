from __future__ import annotations

import logging
import re
import zlib
from collections.abc import AsyncIterator, Callable
from urllib.parse import urljoin, urlparse

import httpx

from .models import ResolverError
from .logging_utils import safe_url_for_log


WRAPPER_PREFIX_BYTES = 3354
DEFAULT_SEGMENT_ORIGIN = "https://vod.xl01.me/"
HLS_SEGMENT_ORIGIN = "https://vod.xl01.me/hls/"
TS_PACKET_SIZE = 188
MAX_SEGMENT_PREFIX_BYTES = 64 * 1024

_URI_ATTRIBUTE_PATTERN = re.compile(r'URI="([^"]+)"')

logger = logging.getLogger(__name__)


def decode_wrapped_manifest(payload: bytes, source_url: str) -> str:
    """Decode the site's PNG/gzip-looking manifest into standard HLS text."""
    if payload.lstrip().startswith(b"#EXTM3U"):
        decoded = payload.decode("utf-8-sig")
        wrapper_mode = "plain"
    else:
        if len(payload) <= WRAPPER_PREFIX_BYTES:
            raise ResolverError("Wrapped manifest is shorter than its expected prefix")
        try:
            decoded = zlib.decompress(
                payload[WRAPPER_PREFIX_BYTES:],
                wbits=zlib.MAX_WBITS | 32,
            ).decode("utf-8-sig")
            wrapper_mode = "wrapped"
        except (zlib.error, UnicodeDecodeError) as exc:
            raise ResolverError("Could not inflate the wrapped manifest") from exc

    if not decoded.lstrip().startswith("#EXTM3U"):
        raise ResolverError("Decoded response is not an HLS manifest")
    logger.debug(
        "event=manifest_payload_decoded source=%s mode=%s input_bytes=%d "
        "decoded_bytes=%d",
        safe_url_for_log(source_url),
        wrapper_mode,
        len(payload),
        len(decoded.encode()),
    )
    return rewrite_manifest(decoded, source_url)


def rewrite_manifest(
    manifest: str,
    source_url: str,
    segment_url_builder: Callable[[str], str] | None = None,
) -> str:
    segment_origin = (
        HLS_SEGMENT_ORIGIN
        if "2014-us" in source_url or "ac5634-us" in source_url
        else DEFAULT_SEGMENT_ORIGIN
    )
    rewritten: list[str] = []
    for line in manifest.splitlines():
        stripped = line.strip()
        if not stripped:
            rewritten.append(line)
        elif stripped.startswith("#"):
            rewritten.append(_rewrite_uri_attribute(line, source_url))
        else:
            absolute = _absolute_media_url(stripped, source_url, segment_origin)
            if segment_url_builder and _is_ts_url(absolute):
                absolute = segment_url_builder(absolute)
            rewritten.append(absolute)
    return "\n".join(rewritten) + "\n"


def first_playlist_uri(manifest: str) -> str | None:
    """Return the first variant or media URI from a standard HLS manifest."""
    for line in manifest.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return None


def find_mpeg_ts_offset(payload: bytes) -> int | None:
    """Find three aligned MPEG-TS packets, including inside a fake PNG."""
    required = TS_PACKET_SIZE * 2
    for offset in range(max(0, len(payload) - required)):
        if (
            payload[offset] == 0x47
            and payload[offset + TS_PACKET_SIZE] == 0x47
            and payload[offset + required] == 0x47
        ):
            return offset
    return None


async def prepare_mpeg_ts_stream(
    upstream: httpx.Response,
) -> tuple[bytes, AsyncIterator[bytes]]:
    """Consume only the wrapper prefix and return clean TS bytes plus the remainder."""
    iterator = upstream.aiter_raw().__aiter__()
    prefix = bytearray()
    offset = None
    while len(prefix) < MAX_SEGMENT_PREFIX_BYTES:
        try:
            prefix.extend(await anext(iterator))
        except StopAsyncIteration:
            break
        offset = find_mpeg_ts_offset(prefix)
        if offset is not None:
            break
    if offset is None:
        logger.warning(
            "event=segment_sync_not_found target=%s inspected_bytes=%d",
            safe_url_for_log(upstream.request.url),
            len(prefix),
        )
        raise ResolverError("Could not find MPEG-TS data in segment")
    logger.debug(
        "event=segment_sync_found target=%s wrapper_bytes=%d inspected_bytes=%d",
        safe_url_for_log(upstream.request.url),
        offset,
        len(prefix),
    )
    return bytes(prefix[offset:]), iterator


async def iter_aligned_mpeg_ts(
    first_chunk: bytes,
    iterator: AsyncIterator[bytes],
    *,
    source_url: str,
) -> AsyncIterator[bytes]:
    """Yield only complete, synchronized 188-byte MPEG-TS packets."""
    pending = bytearray(first_chunk)
    discarded = 0

    async def emit_ready_packets() -> AsyncIterator[bytes]:
        nonlocal discarded
        while len(pending) >= TS_PACKET_SIZE:
            if pending[0] != 0x47:
                offset = find_mpeg_ts_offset(pending)
                if offset is None:
                    if len(pending) > MAX_SEGMENT_PREFIX_BYTES:
                        keep = TS_PACKET_SIZE * 3 - 1
                        drop = len(pending) - keep
                        del pending[:drop]
                        discarded += drop
                    return
                del pending[:offset]
                discarded += offset

            packet_count = len(pending) // TS_PACKET_SIZE
            valid_count = 0
            while (
                valid_count < packet_count
                and pending[valid_count * TS_PACKET_SIZE] == 0x47
            ):
                valid_count += 1
            if valid_count == 0:
                return
            end = valid_count * TS_PACKET_SIZE
            yield bytes(pending[:end])
            del pending[:end]

    async for ready in emit_ready_packets():
        yield ready
    async for chunk in iterator:
        pending.extend(chunk)
        async for ready in emit_ready_packets():
            yield ready

    async for ready in emit_ready_packets():
        yield ready
    discarded += len(pending)
    if discarded:
        logger.debug(
            "event=segment_non_ts_discarded target=%s bytes=%d",
            safe_url_for_log(source_url),
            discarded,
        )


def _rewrite_uri_attribute(line: str, source_url: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return f'URI="{urljoin(source_url, match.group(1))}"'

    return _URI_ATTRIBUTE_PATTERN.sub(replace, line)


def _absolute_media_url(line: str, source_url: str, segment_origin: str) -> str:
    if urlparse(line).scheme in {"http", "https"}:
        return line
    if _is_ts_url(line):
        return urljoin(segment_origin, line.lstrip("/"))
    return urljoin(source_url, line)


def _is_ts_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".ts")
