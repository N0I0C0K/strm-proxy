from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse, StreamingResponse

from .dependencies import (
    HttpClientDep,
    PlaybackCoordinatorDep,
    ResolverDep,
    SegmentCacheDep,
    SettingsDep,
)
from .hls import iter_aligned_mpeg_ts, prepare_mpeg_ts_stream, rewrite_manifest
from .logging_utils import safe_url_for_log
from .models import ResolverError
from .playback_selection import PlaybackUnavailable
from .segment_cache import SegmentClaim, SegmentPayload

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/resolve")
async def resolve(resolver: ResolverDep, page_url: str = Query(...)) -> dict:
    resolved = await resolver.resolve_page(page_url)
    logger.info(
        "event=resolve_response pid=%d hls_lines=%d tos=%s member=%s",
        resolved.pid,
        len(resolved.candidates),
        resolved.tos_available,
        resolved.member_token is not None,
    )
    return {
        "page_url": resolved.page_url,
        "pid": resolved.pid,
        "title": resolved.title,
        "sources": {
            "hls": bool(resolved.candidates),
            "tos": resolved.tos_available,
            "member": resolved.member_token is not None,
        },
        "lines": [
            {"index": index, "kind": candidate.kind, "url": candidate.url}
            for index, candidate in enumerate(resolved.candidates)
        ],
    }


@router.get("/play", name="play")
@router.head("/play", include_in_schema=False, name="play_head")
async def play(
    request: Request,
    resolver: ResolverDep,
    playback: PlaybackCoordinatorDep,
    page_url: str = Query(...),
    source: Literal["auto", "hls", "tos", "member"] = Query("auto"),
    line: int = Query(0, ge=0),
) -> RedirectResponse:
    logger.info(
        "event=play_request page=%s source=%s preferred_line=%d",
        safe_url_for_log(page_url),
        source,
        line,
    )

    if source == "auto":
        try:
            result = await playback.resolve_auto(
                resolver,
                page_url,
                preferred_line=line,
            )
        except PlaybackUnavailable as exc:
            logger.warning(
                "event=play_failed page=%s source=auto preferred_line=%d "
                "cached=%s detail=%s",
                safe_url_for_log(page_url),
                line,
                exc.cached,
                exc,
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "no_playable_source",
                    "message": "All advertised playback sources failed",
                    "errors": list(exc.errors),
                },
                headers={
                    "Retry-After": str(exc.retry_after_seconds),
                    "X-STRM-Proxy-Error": "no-playable-source",
                },
            ) from exc

        selection = result.selection
        if selection.source == "hls":
            assert selection.line is not None
            logger.info(
                "event=play_selected pid=%d mode=hls line=%d "
                "line_count=%d strategy=%s",
                result.resolved.pid,
                selection.line,
                len(result.resolved.candidates),
                "selection_cache" if result.cache_hit else "explored",
            )
            return _no_store_redirect(
                build_manifest_url(request, page_url, selection.line)
            )

        assert result.media_url is not None
        logger.info(
            "event=play_selected pid=%d mode=direct source=%s target=%s "
            "strategy=%s",
            result.resolved.pid,
            selection.source,
            safe_url_for_log(result.media_url),
            "selection_cache" if result.cache_hit else "explored",
        )
        return _no_store_redirect(result.media_url)

    resolved = await resolver.resolve_page(page_url)
    if source in {"tos", "member"}:
        try:
            _resolved, media_url = await resolver.resolve_direct_media(
                page_url,
                source,
            )
        except ResolverError as exc:
            logger.warning(
                "event=play_direct_fallback pid=%d source=%s detail=%s",
                resolved.pid,
                source,
                exc,
            )
            raise
        logger.info(
            "event=play_selected pid=%d mode=direct source=%s target=%s",
            resolved.pid,
            source,
            safe_url_for_log(media_url),
        )
        return _no_store_redirect(media_url)

    if source == "hls":
        if line < len(resolved.candidates):
            logger.info(
                "event=play_selected pid=%d mode=hls-explicit line=%d "
                "line_count=%d",
                resolved.pid,
                line,
                len(resolved.candidates),
            )
            return _no_store_redirect(build_manifest_url(request, page_url, line))
        if not resolved.candidates:
            detail = "hls: no HLS source was advertised"
        else:
            detail = (
                f"hls: line {line} does not exist; "
                f"found {len(resolved.candidates)} line(s)"
            )
        logger.warning(
            "event=play_failed pid=%d source=%s preferred_line=%d detail=%s",
            resolved.pid,
            source,
            line,
            detail,
        )
        raise HTTPException(status_code=502, detail=detail)

    raise AssertionError(f"Unexpected playback source: {source}")


@router.get("/hls.m3u8", name="hls_manifest")
async def hls_manifest(
    request: Request,
    resolver: ResolverDep,
    settings: SettingsDep,
    playback: PlaybackCoordinatorDep,
    segment_cache: SegmentCacheDep,
    page_url: str = Query(...),
    line: int = Query(0, ge=0),
) -> Response:
    resolved = await resolver.resolve_page(page_url)
    try:
        _resolved, candidate, manifest = await resolver.fetch_manifest(
            page_url=page_url,
            line=line,
        )
    except (ResolverError, httpx.HTTPError):
        playback.invalidate_hls(
            resolved,
            line,
            reason="manifest_fetch_failed",
        )
        raise
    if settings.proxy_segments:
        segment_endpoint = request.url_for("proxy_segment")
        playlist_id = segment_cache.register_playlist(page_url, line, manifest)
        segment_index = 0

        def segment_builder(segment_url: str) -> str:
            nonlocal segment_index
            parameters: dict[str, str | int] = {
                "url": segment_url,
                "referer": page_url,
            }
            if playlist_id is not None:
                parameters["playlist"] = playlist_id
                parameters["index"] = segment_index
            segment_index += 1
            return str(segment_endpoint.include_query_params(**parameters))

        manifest = rewrite_manifest(
            manifest,
            getattr(candidate, "url", page_url),
            segment_builder,
        )
    logger.info(
        "event=hls_manifest_served pid=%s line=%d proxy_segments=%s bytes=%d",
        getattr(_resolved, "pid", "unknown"),
        line,
        settings.proxy_segments,
        len(manifest.encode()),
    )
    return Response(
        content=manifest,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/strm")
async def strm(
    request: Request,
    page_url: str = Query(...),
    source: Literal["auto", "hls", "tos", "member"] = Query("auto"),
    line: int = Query(0, ge=0),
) -> PlainTextResponse:
    play_url = build_play_url(request, page_url, source, line)
    logger.debug(
        "event=strm_generated page=%s source=%s line=%d target=%s",
        safe_url_for_log(page_url),
        source,
        line,
        safe_url_for_log(play_url),
    )
    return PlainTextResponse(play_url + "\n")


@router.get("/segment", name="proxy_segment")
@router.head("/segment", include_in_schema=False, name="proxy_segment_head")
async def proxy_segment(
    request: Request,
    client: HttpClientDep,
    settings: SettingsDep,
    segment_cache: SegmentCacheDep,
    url: str = Query(...),
    referer: str | None = Query(None),
    playlist: str | None = Query(None),
    index: int | None = Query(None, ge=0),
) -> Response:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Only {settings.segment_host} segments are allowed",
        ) from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != settings.segment_host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Only {settings.segment_host} segments are allowed",
        )

    if request.method == "GET":
        segment_cache.start_prefetch(
            playlist,
            index,
            requested_url=url,
        )

    claim: SegmentClaim | None = None
    if segment_cache.enabled:
        if request.method == "HEAD":
            cached = segment_cache.cached(url, referer)
            if cached is not None:
                return _cached_segment_response(cached, head=True)
        else:
            while True:
                claim = segment_cache.claim(url, referer)
                if claim.payload is not None:
                    return _cached_segment_response(claim.payload)
                if claim.waiter is None:
                    break
                payload = await asyncio.shield(claim.waiter)
                if payload is not None:
                    return _cached_segment_response(payload)

    headers = {"Referer": referer} if referer else {}
    logger.debug(
        "event=segment_request method=%s target=%s referer_present=%s",
        request.method,
        safe_url_for_log(url),
        referer is not None,
    )
    upstream_request = client.build_request(request.method, url, headers=headers)
    try:
        upstream = await client.send(upstream_request, stream=True)
    except BaseException:
        if claim is not None:
            segment_cache.fail(claim)
        raise
    response_headers = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower() == "cache-control"
    }
    if request.method == "HEAD":
        await upstream.aclose()
        return Response(
            status_code=upstream.status_code,
            headers=response_headers,
            media_type="video/mp2t",
        )
    if upstream.is_error:
        status_code = upstream.status_code
        logger.warning(
            "event=segment_upstream_error target=%s status=%d",
            safe_url_for_log(url),
            status_code,
        )
        await upstream.aclose()
        if claim is not None:
            segment_cache.fail(claim)
        raise HTTPException(
            status_code=502,
            detail=f"Segment upstream returned HTTP {status_code}",
        )

    try:
        first_chunk, iterator = await prepare_mpeg_ts_stream(upstream)
    except Exception:
        await upstream.aclose()
        if claim is not None:
            segment_cache.fail(claim)
        raise

    async def decoded_stream() -> AsyncIterator[bytes]:
        buffered = bytearray()
        cacheable = claim is not None and claim.owner
        completed = False
        try:
            async for chunk in iter_aligned_mpeg_ts(
                first_chunk,
                iterator,
                source_url=url,
            ):
                if cacheable:
                    if len(buffered) + len(chunk) <= segment_cache.max_bytes:
                        buffered.extend(chunk)
                    else:
                        buffered.clear()
                        cacheable = False
                yield chunk
            completed = True
        finally:
            await upstream.aclose()
            if claim is not None:
                payload = None
                if completed and cacheable:
                    payload = SegmentPayload(
                        data=bytes(buffered),
                        cache_control=response_headers.get("cache-control"),
                    )
                segment_cache.complete_foreground(claim, payload)

    return StreamingResponse(
        decoded_stream(),
        status_code=200,
        media_type="video/mp2t",
        headers=response_headers,
    )


def build_manifest_url(
    request: Request,
    page_url: str,
    line: int = 0,
) -> str:
    return str(
        request.url_for("hls_manifest").include_query_params(
            page_url=page_url,
            line=line,
        )
    )


def build_play_url(
    request: Request,
    page_url: str,
    source: str = "auto",
    line: int = 0,
) -> str:
    url = request.url_for("play").include_query_params(page_url=page_url)
    if source != "auto":
        url = url.include_query_params(source=source)
    if line != 0:
        url = url.include_query_params(line=line)
    return str(url)


def _no_store_redirect(url: str) -> RedirectResponse:
    return RedirectResponse(
        url=url,
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


def _cached_segment_response(
    payload: SegmentPayload,
    *,
    head: bool = False,
) -> Response:
    headers = {"Content-Length": str(len(payload.data))}
    if payload.cache_control is not None:
        headers["Cache-Control"] = payload.cache_control
    return Response(
        content=b"" if head else payload.data,
        status_code=200,
        media_type="video/mp2t",
        headers=headers,
    )
