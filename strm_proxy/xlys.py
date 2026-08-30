from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Iterable
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from .cache import TTLCache
from .hls import decode_wrapped_manifest, rewrite_manifest
from .models import ResolvedPage, ResolverError, StreamCandidate


_PID_PATTERN = re.compile(r"\bvar\s+pid\s*=\s*(\d+)\s*;")
_TITLE_PATTERN = re.compile(r"\bvod_name\s*=\s*([\"'])(.*?)\1")
_PLAY_PATH_PATTERN = re.compile(r"^/(?:[^/?#]+/)?play/\d+-\d+\.htm$")
_UNICODE_ESCAPE_PATTERN = re.compile(r"\\u([0-9a-fA-F]{4})")

ManifestResult = tuple[ResolvedPage, StreamCandidate, str]


def create_signature(pid: int, timestamp_ms: int) -> str:
    """Reproduce the site's AES-ECB signature for its /lines endpoint."""
    message = f"{pid}-{timestamp_ms}".encode()
    key = hashlib.md5(message).hexdigest()[:16].encode()
    encrypted = AES.new(key, AES.MODE_ECB).encrypt(pad(message, AES.block_size))
    return encrypted.hex().upper()


def validate_page_url(page_url: str, allowed_hosts: Iterable[str]) -> str:
    parsed = urlparse(page_url)
    allowed = {host.lower() for host in allowed_hosts}
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed:
        raise ResolverError("Only HTTPS URLs from the configured xlys host are allowed")
    if parsed.username or parsed.password or parsed.port:
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
    """Resolve xlys play pages and cache their standard HLS manifests."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        allowed_hosts: Iterable[str] = ("www.xlys02.com", "xlys02.com"),
        cache_ttl_seconds: float = 300,
    ) -> None:
        self.client = client
        self.allowed_hosts = tuple(allowed_hosts)
        self._page_cache = TTLCache[str, ResolvedPage](cache_ttl_seconds)
        self._manifest_cache = TTLCache[tuple[str, int], ManifestResult](
            cache_ttl_seconds
        )

    async def resolve_page(self, page_url: str) -> ResolvedPage:
        page_url = validate_page_url(page_url, self.allowed_hosts)
        if cached := self._page_cache.get(page_url):
            return cached

        origin = _origin(page_url)
        page_response = await self.client.get(
            page_url,
            headers={"Referer": f"{origin}/"},
        )
        page_response.raise_for_status()
        pid, title = parse_page(page_response.text, page_url)
        candidates = await self._fetch_candidates(origin, page_url, pid)
        resolved = ResolvedPage(
            page_url=page_url,
            pid=pid,
            title=title,
            candidates=candidates,
        )
        self._page_cache.set(page_url, resolved)
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
            return _with_segment_builder(cached, segment_url_builder)

        resolved = await self.resolve_page(page_url)
        try:
            candidate = resolved.candidates[line]
        except IndexError as exc:
            raise ResolverError(
                f"Line {line} does not exist; found {len(resolved.candidates)} line(s)"
            ) from exc

        upstream_url, _fragment = urldefrag(candidate.url)
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
        return _with_segment_builder(result, segment_url_builder)

    async def _fetch_candidates(
        self,
        origin: str,
        page_url: str,
        pid: int,
    ) -> tuple[StreamCandidate, ...]:
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
        try:
            body = response.json()
        except ValueError as exc:
            raise ResolverError("The /lines endpoint did not return JSON") from exc
        if body.get("code") != 0 or not isinstance(body.get("data"), dict):
            raise ResolverError(f"The /lines endpoint rejected the request: {body!r}")
        candidates = extract_candidates(body["data"], origin)
        if not candidates:
            raise ResolverError("No wrapped M3U8 candidate was returned")
        return candidates


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
