from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Callable, Iterable
from typing import Literal
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from .cache import TTLCache
from .hls import (
    MAX_SEGMENT_PREFIX_BYTES,
    decode_wrapped_manifest,
    find_mpeg_ts_offset,
    first_playlist_uri,
    rewrite_manifest,
)
from .logging_utils import describe_http_error, safe_url_for_log
from .models import ResolvedPage, ResolverError, StreamCandidate


_PID_PATTERN = re.compile(r"\bvar\s+pid\s*=\s*(\d+)\s*;")
_TITLE_PATTERN = re.compile(r"\bvod_name\s*=\s*([\"'])(.*?)\1")
_PLAY_PATH_PATTERN = re.compile(r"^/(?:[^/?#]+/)?play/\d+-\d+\.htm$")
_UNICODE_ESCAPE_PATTERN = re.compile(r"\\u([0-9a-fA-F]{4})")

ManifestResult = tuple[ResolvedPage, StreamCandidate, str]
DirectMediaResult = tuple[ResolvedPage, str]
HlsMediaKind = Literal["raw", "wrapped", "unhealthy"]

_DIRECT_MEDIA_MARKERS = (
    "tos-alisg-v-0000",
    "tos-o-maliva-0000-us",
    "tos-alisg-i-0000",
)
_DIRECT_MEDIA_HOST_SUFFIXES = (
    "ibyteimg.com",
    "bytegecko-i18n.com",
    "bytepluscdn.com",
    "fdmimgs.com",
    "faceulv.com",
    "faceueditorv.com",
    "bitssesc.com",
    "geckocdn.com",
    "anyweb.space",
    "capcutstatic.com",
    "akamaized.net",
    "tiktokcdn.com",
)
_CANONICAL_TOS_ORIGIN = "https://p16-hera-sign-va.ibyteimg.com/obj/"

logger = logging.getLogger(__name__)


def create_signature(pid: int, timestamp_ms: int) -> str:
    """Reproduce the site's AES-ECB signature for its /lines endpoint."""
    message = f"{pid}-{timestamp_ms}".encode()
    key = hashlib.md5(message).hexdigest()[:16].encode()
    encrypted = AES.new(key, AES.MODE_ECB).encrypt(pad(message, AES.block_size))
    return encrypted.hex().upper()


def validate_page_url(page_url: str, allowed_hosts: Iterable[str]) -> str:
    try:
        parsed = urlparse(page_url)
        port = parsed.port
    except ValueError as exc:
        raise ResolverError("The play page URL is invalid") from exc
    allowed = {host.lower() for host in allowed_hosts}
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed:
        raise ResolverError("Only HTTPS URLs from the configured xlys host are allowed")
    if parsed.username or parsed.password or port is not None:
        raise ResolverError("Credentials and custom ports are not allowed")
    if not _PLAY_PATH_PATTERN.fullmatch(parsed.path):
        raise ResolverError(
            "Expected a page URL like /play/27062-0.htm "
            "or /guoju/play/24358-0.htm"
        )
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def parse_page(html: str, page_url: str) -> tuple[int, str | None]:
    pid_match = _PID_PATTERN.search(html)
    if not pid_match:
        raise ResolverError(f"Could not find pid in {page_url}")
    title_match = _TITLE_PATTERN.search(html)
    title = title_match.group(2) if title_match else None
    if title:
        title = _UNICODE_ESCAPE_PATTERN.sub(
            lambda match: chr(int(match.group(1), 16)),
            title,
        )
    return int(pid_match.group(1)), title


def extract_candidates(data: dict, origin: str) -> tuple[StreamCandidate, ...]:
    candidates: list[StreamCandidate] = []
    for kind in ("m3u8", "m3u8_2", "url3"):
        value = data.get(kind)
        if not isinstance(value, str):
            continue
        for raw_url in value.split(","):
            normalized = normalize_candidate_url(raw_url.strip(), origin)
            if normalized and ".m3u8" in normalized.lower():
                candidates.append(StreamCandidate(kind=kind, url=normalized))

    unique: list[StreamCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.url not in seen:
            unique.append(candidate)
            seen.add(candidate.url)
    return tuple(unique)


def normalize_candidate_url(candidate_url: str, origin: str) -> str:
    if not candidate_url:
        return ""
    parsed_origin = urlparse(origin)
    candidate_url = candidate_url.replace("www.xlys01.com", parsed_origin.netloc)
    candidate_url = candidate_url.replace("www.bde4.cc", parsed_origin.netloc)
    candidate_url = candidate_url.replace("vod.xlys01.com", parsed_origin.netloc)
    if candidate_url.startswith("//"):
        return f"{parsed_origin.scheme}:{candidate_url}"
    if candidate_url.startswith("/"):
        return urljoin(origin, candidate_url)
    return candidate_url


class XlysResolver:
    """Resolve xlys play pages into verified direct media or HLS manifests."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        allowed_hosts: Iterable[str] = ("www.xlys02.com", "xlys02.com"),
        cache_ttl_seconds: float = 300,
        member_access_enabled: bool = False,
        segment_hosts: Iterable[str] = ("vod.xl01.me",),
        prefer_raw_segments: bool = True,
    ) -> None:
        self.client = client
        self.allowed_hosts = tuple(allowed_hosts)
        self.segment_hosts = tuple(segment_hosts)
        self.member_access_enabled = member_access_enabled
        self.prefer_raw_segments = prefer_raw_segments
        self._page_cache = TTLCache[str, ResolvedPage](cache_ttl_seconds)
        self._manifest_cache = TTLCache[tuple[str, int], ManifestResult](
            cache_ttl_seconds
        )

    async def resolve_page(
        self,
        page_url: str,
        *,
        refresh: bool = False,
    ) -> ResolvedPage:
        page_url = validate_page_url(page_url, self.allowed_hosts)
        if not refresh and (cached := self._page_cache.get(page_url)):
            logger.debug(
                "event=page_cache_hit page=%s pid=%d",
                safe_url_for_log(page_url),
                cached.pid,
            )
            return cached

        origin = _origin(page_url)
        logger.debug("event=page_fetch page=%s", safe_url_for_log(page_url))
        page_response = await self.client.get(
            page_url,
            headers={"Referer": f"{origin}/"},
        )
        page_response.raise_for_status()
        pid, title = parse_page(page_response.text, page_url)
        candidates, tos_available, member_token = await self._fetch_candidates(
            origin,
            page_url,
            pid,
        )
        resolved = ResolvedPage(
            page_url=page_url,
            pid=pid,
            title=title,
            candidates=candidates,
            tos_available=tos_available,
            member_token=member_token,
        )
        self._page_cache.set(page_url, resolved)
        logger.info(
            "event=page_resolved pid=%d title=%r hls_lines=%d tos=%s member=%s",
            resolved.pid,
            resolved.title,
            len(resolved.candidates),
            resolved.tos_available,
            resolved.member_token is not None,
        )
        return resolved

    async def fetch_manifest(
        self,
        page_url: str,
        line: int = 0,
        segment_url_builder: Callable[[str], str] | None = None,
    ) -> ManifestResult:
        if line < 0:
            raise ResolverError("Line must be zero or greater")
        page_url = validate_page_url(page_url, self.allowed_hosts)
        cache_key = (page_url, line)
        cached = self._manifest_cache.get(cache_key)
        if cached:
            logger.debug(
                "event=manifest_cache_hit page=%s line=%d",
                safe_url_for_log(page_url),
                line,
            )
            return _with_segment_builder(cached, segment_url_builder)

        resolved = await self.resolve_page(page_url)
        try:
            candidate = resolved.candidates[line]
        except IndexError as exc:
            raise ResolverError(
                f"Line {line} does not exist; found {len(resolved.candidates)} line(s)"
            ) from exc

        upstream_url, _fragment = urldefrag(candidate.url)
        logger.debug(
            "event=manifest_fetch pid=%d line=%d kind=%s upstream=%s",
            resolved.pid,
            line,
            candidate.kind,
            safe_url_for_log(upstream_url),
        )
        response = await self.client.get(
            upstream_url,
            headers={"Referer": resolved.page_url},
        )
        response.raise_for_status()
        result = (
            resolved,
            candidate,
            decode_wrapped_manifest(response.content, candidate.url),
        )
        self._manifest_cache.set(cache_key, result)
        logger.debug(
            "event=manifest_decoded pid=%d line=%d upstream_status=%d "
            "wrapped_bytes=%d manifest_bytes=%d",
            resolved.pid,
            line,
            response.status_code,
            len(response.content),
            len(result[2].encode()),
        )
        return _with_segment_builder(result, segment_url_builder)

    async def resolve_direct_media(
        self,
        page_url: str,
        source: str,
    ) -> DirectMediaResult:
        if source not in {"tos", "member"}:
            raise ResolverError(f"Unsupported direct media source: {source}")

        resolved = await self.resolve_page(page_url)
        logger.info(
            "event=direct_source_attempt pid=%d source=%s",
            resolved.pid,
            source,
        )
        if source == "tos":
            if not resolved.tos_available:
                raise ResolverError("The play page does not advertise a TOS source")
            endpoint_suffix = "?type=1"
            verify_code = "888"
        else:
            if not self.member_access_enabled:
                raise ResolverError(
                    "The member source requires xlys login credentials"
                )
            if not resolved.member_token:
                raise ResolverError(
                    "The play page does not advertise a member source"
                )
            raise ResolverError(
                "The member source requires automatic CAPTCHA recognition, "
                "which is not configured"
            )

        raw_url = await self._fetch_god_media_url(
            resolved,
            endpoint_suffix=endpoint_suffix,
            verify_code=verify_code,
        )
        for media_url in direct_media_url_candidates(raw_url):
            logger.debug(
                "event=direct_media_probe pid=%d source=%s target=%s",
                resolved.pid,
                source,
                safe_url_for_log(media_url),
            )
            if await self._probe_direct_media(media_url):
                logger.info(
                    "event=direct_source_selected pid=%d source=%s target=%s",
                    resolved.pid,
                    source,
                    safe_url_for_log(media_url),
                )
                return resolved, media_url
        raise ResolverError(
            f"The {source} source did not return a directly playable media object"
        )

    async def find_working_hls_line(
        self,
        page_url: str,
        preferred_line: int = 0,
    ) -> int:
        resolved = await self.resolve_page(page_url)
        line_count = len(resolved.candidates)
        if line_count == 0:
            raise ResolverError("The play page does not advertise an HLS source")

        order = list(range(line_count))
        if 0 <= preferred_line < line_count:
            order.remove(preferred_line)
            order.insert(0, preferred_line)

        logger.info(
            "event=hls_probe_start pid=%d line_count=%d preferred_line=%d "
            "prefer_raw_segments=%s strategy=parallel order=%s",
            resolved.pid,
            line_count,
            preferred_line,
            self.prefer_raw_segments,
            ",".join(str(item) for item in order),
        )

        failures: list[str] = []
        wrapped_fallback: int | None = None
        tasks = {
            line: asyncio.create_task(self._probe_hls_line(page_url, line))
            for line in order
        }
        try:
            while tasks:
                done, _pending = await asyncio.wait(
                    tasks.values(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for line in order:
                    task = tasks.get(line)
                    if task not in done:
                        continue
                    del tasks[line]
                    try:
                        media_kind = task.result()
                    except (httpx.HTTPError, ResolverError) as exc:
                        detail = (
                            describe_http_error(exc)
                            if isinstance(exc, httpx.HTTPError)
                            else str(exc)
                        )
                        failures.append(f"line {line}: {detail}")
                        logger.warning(
                            "event=hls_line_probe_error pid=%d line=%d "
                            "kind=%s detail=%s",
                            resolved.pid,
                            line,
                            resolved.candidates[line].kind,
                            detail,
                        )
                        continue

                    if media_kind == "raw" or (
                        media_kind == "wrapped"
                        and not self.prefer_raw_segments
                    ):
                        logger.info(
                            "event=hls_line_selected pid=%d line=%d kind=%s "
                            "media_kind=%s fallback=false",
                            resolved.pid,
                            line,
                            resolved.candidates[line].kind,
                            media_kind,
                        )
                        return line
                    if media_kind == "wrapped":
                        if (
                            wrapped_fallback is None
                            or order.index(line) < order.index(wrapped_fallback)
                        ):
                            wrapped_fallback = line
                        logger.info(
                            "event=hls_line_deferred pid=%d line=%d kind=%s "
                            "reason=wrapped_segments",
                            resolved.pid,
                            line,
                            resolved.candidates[line].kind,
                        )
                        continue

                    failures.append(
                        f"line {line}: first media resource is unavailable"
                    )
                    logger.info(
                        "event=hls_line_unhealthy pid=%d line=%d kind=%s",
                        resolved.pid,
                        line,
                        resolved.candidates[line].kind,
                    )
        finally:
            pending = tuple(tasks.values())
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if wrapped_fallback is not None:
            logger.info(
                "event=hls_line_selected pid=%d line=%d kind=%s "
                "media_kind=wrapped fallback=true",
                resolved.pid,
                wrapped_fallback,
                resolved.candidates[wrapped_fallback].kind,
            )
            return wrapped_fallback
        raise ResolverError(
            "No healthy HLS line was found (" + "; ".join(failures) + ")"
        )

    async def _fetch_candidates(
        self,
        origin: str,
        page_url: str,
        pid: int,
    ) -> tuple[tuple[StreamCandidate, ...], bool, str | None]:
        timestamp_ms = int(time.time() * 1000)
        response = await self.client.get(
            f"{origin}/lines",
            params={
                "t": timestamp_ms,
                "sg": create_signature(pid, timestamp_ms),
                "pid": pid,
            },
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": page_url,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        response.raise_for_status()
        logger.debug(
            "event=lines_response pid=%d status=%d",
            pid,
            response.status_code,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise ResolverError("The /lines endpoint did not return JSON") from exc
        if body.get("code") != 0 or not isinstance(body.get("data"), dict):
            raise ResolverError(f"The /lines endpoint rejected the request: {body!r}")
        candidates = extract_candidates(body["data"], origin)
        tos_available = bool(body["data"].get("tos"))
        raw_member_token = body["data"].get("ptoken")
        member_token = (
            str(raw_member_token).strip()
            if raw_member_token not in (None, "", False)
            else None
        )
        if not candidates and not tos_available and member_token is None:
            raise ResolverError("No playable source was returned")
        logger.info(
            "event=lines_parsed pid=%d hls_lines=%d tos=%s member=%s",
            pid,
            len(candidates),
            tos_available,
            member_token is not None,
        )
        return candidates, tos_available, member_token

    async def _fetch_god_media_url(
        self,
        resolved: ResolvedPage,
        *,
        endpoint_suffix: str,
        verify_code: str,
    ) -> str:
        timestamp_ms = int(time.time() * 1000)
        origin = _origin(resolved.page_url)
        response = await self.client.post(
            f"{origin}/god/{resolved.pid}{endpoint_suffix}",
            data={
                "t": timestamp_ms,
                "sg": create_signature(resolved.pid, timestamp_ms),
                "verifyCode": verify_code,
            },
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": resolved.page_url,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        response.raise_for_status()
        logger.debug(
            "event=god_response pid=%d endpoint_type=%s status=%d",
            resolved.pid,
            "tos" if endpoint_suffix else "member",
            response.status_code,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise ResolverError("The /god endpoint did not return JSON") from exc
        media_url = body.get("url") if isinstance(body, dict) else None
        if not isinstance(media_url, str) or not media_url.strip():
            error = body.get("error") if isinstance(body, dict) else None
            raise ResolverError(
                f"The /god endpoint did not return a media URL: {error or body!r}"
            )
        media_url = media_url.strip()
        logger.debug(
            "event=god_media_url pid=%d target=%s",
            resolved.pid,
            safe_url_for_log(media_url),
        )
        return media_url

    async def _probe_direct_media(self, media_url: str) -> bool:
        try:
            async with self.client.stream(
                "GET",
                media_url,
                headers={"Range": "bytes=0-63"},
            ) as response:
                if response.status_code not in {200, 206}:
                    logger.debug(
                        "event=direct_media_probe_result target=%s status=%d",
                        safe_url_for_log(media_url),
                        response.status_code,
                    )
                    return False
                prefix = b""
                async for chunk in response.aiter_bytes():
                    prefix += chunk
                    if len(prefix) >= 64:
                        break
        except httpx.HTTPError:
            return False

        content_type = response.headers.get("content-type", "").lower()
        healthy = content_type.startswith("video/") or b"ftyp" in prefix[:32]
        logger.debug(
            "event=direct_media_probe_result target=%s status=%d "
            "content_type=%s healthy=%s",
            safe_url_for_log(media_url),
            response.status_code,
            content_type or "unknown",
            healthy,
        )
        return healthy

    async def _probe_hls_line(self, page_url: str, line: int) -> HlsMediaKind:
        _resolved, candidate, manifest = await self.fetch_manifest(page_url, line)
        current_url = candidate.url
        for _depth in range(3):
            media_url = first_playlist_uri(manifest)
            if media_url is None:
                raise ResolverError(f"HLS line {line} contains no media URI")
            if urlparse(media_url).path.lower().endswith(".m3u8"):
                if not self._is_allowed_hls_probe_url(media_url, manifest=True):
                    raise ResolverError("HLS variant URL uses an untrusted host")
                current_url, _fragment = urldefrag(media_url)
                response = await self.client.get(
                    current_url,
                    headers={"Referer": page_url},
                )
                response.raise_for_status()
                logger.debug(
                    "event=hls_variant_fetched line=%d depth=%d target=%s "
                    "status=%d",
                    line,
                    _depth + 1,
                    safe_url_for_log(current_url),
                    response.status_code,
                )
                manifest = decode_wrapped_manifest(response.content, media_url)
                continue
            if not self._is_allowed_hls_probe_url(media_url, manifest=False):
                raise ResolverError("HLS media URL uses an untrusted host")
            return await self._probe_hls_media(media_url, page_url)
        raise ResolverError(f"HLS line {line} contains too many nested playlists")

    async def _probe_hls_media(
        self,
        media_url: str,
        page_url: str,
    ) -> HlsMediaKind:
        try:
            async with self.client.stream(
                "GET",
                media_url,
                headers={"Referer": page_url},
            ) as response:
                if response.status_code not in {200, 206}:
                    logger.debug(
                        "event=hls_media_probe_result target=%s status=%d",
                        safe_url_for_log(media_url),
                        response.status_code,
                    )
                    return "unhealthy"
                content_type = response.headers.get(
                    "content-type",
                    "",
                ).lower()
                prefix = bytearray()
                ts_offset: int | None = None
                is_fmp4 = False
                async for chunk in response.aiter_bytes(chunk_size=1024):
                    remaining = MAX_SEGMENT_PREFIX_BYTES - len(prefix)
                    prefix.extend(chunk[:remaining])
                    ts_offset = find_mpeg_ts_offset(prefix)
                    is_fmp4 = b"ftyp" in prefix[:32]
                    if (
                        ts_offset is not None
                        or is_fmp4
                        or len(prefix) >= MAX_SEGMENT_PREFIX_BYTES
                    ):
                        break
        except httpx.HTTPError:
            return "unhealthy"

        if ts_offset == 0 or is_fmp4:
            media_kind: HlsMediaKind = "raw"
        elif ts_offset is not None:
            media_kind = "wrapped"
        else:
            media_kind = "unhealthy"
        logger.debug(
            "event=hls_media_probe_result target=%s status=%d "
            "content_type=%s media_kind=%s ts_offset=%s inspected_bytes=%d",
            safe_url_for_log(media_url),
            response.status_code,
            content_type or "unknown",
            media_kind,
            ts_offset if ts_offset is not None else "none",
            len(prefix),
        )
        return media_kind

    def _is_allowed_hls_probe_url(self, url: str, *, manifest: bool) -> bool:
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        allowed_hosts = set(self.segment_hosts)
        if manifest:
            allowed_hosts.update(self.allowed_hosts)
        return (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and port is None
            and host in allowed_hosts
        )


def direct_media_url_candidates(raw_url: str) -> tuple[str, ...]:
    """Return trusted direct-media URLs in the order used for probing."""
    normalized = raw_url.replace(
        "v16m-default.tiktokcdn.com",
        "v16m-default.akamaized.net",
    )
    candidates: list[str] = []
    for marker in _DIRECT_MEDIA_MARKERS:
        marker_index = normalized.find(marker)
        if marker_index >= 0:
            candidates.append(_CANONICAL_TOS_ORIGIN + normalized[marker_index:])
            break
    if _is_trusted_direct_media_url(normalized):
        candidates.append(normalized)
    return tuple(dict.fromkeys(candidates))


def _is_trusted_direct_media_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and port is None
        and any(
            host == suffix or host.endswith(f".{suffix}")
            for suffix in _DIRECT_MEDIA_HOST_SUFFIXES
        )
    )


def _with_segment_builder(
    result: ManifestResult,
    segment_url_builder: Callable[[str], str] | None,
) -> ManifestResult:
    if segment_url_builder is None:
        return result
    resolved, candidate, manifest = result
    return (
        resolved,
        candidate,
        rewrite_manifest(manifest, candidate.url, segment_url_builder),
    )


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"
