from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import logging
import time
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse, StreamingResponse

from .dependencies import (
    HttpClientDep,
    MediaRepositoryDep,
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
            {
                "index": index,
                "name": candidate.route_name,
                "kind": candidate.kind,
                "url": candidate.url,
            }
            for index, candidate in enumerate(resolved.candidates)
        ],
    }


@router.get("/play", name="play")
@router.head("/play", include_in_schema=False, name="play_head")
async def play(
    request: Request,
    resolver: ResolverDep,
    playback: PlaybackCoordinatorDep,
    repository: MediaRepositoryDep,
    page_url: str = Query(...),
    source: Literal["auto", "hls", "tos", "member"] = Query("auto"),
) -> RedirectResponse:
    logger.info(
        "event=play_request page=%s source=%s",
        safe_url_for_log(page_url),
        source,
    )

    if source == "auto":
        try:
            result = await playback.resolve_auto(resolver, page_url)
        except PlaybackUnavailable as exc:
            logger.warning(
                "event=play_failed page=%s source=auto cached=%s detail=%s",
                safe_url_for_log(page_url),
                exc.cached,
                exc,
            )
            _raise_playback_unavailable(exc)

        selection = result.selection
        if request.method == "GET":
            repository.mark_series_watched(page_url)
        if selection.source == "hls":
            assert selection.line is not None
            assert selection.revision is not None
            logger.info(
                "event=play_selected pid=%d mode=hls line=%d route=%s "
                "line_count=%d strategy=%s",
                result.resolved.pid,
                selection.line,
                selection.route_name,
                len(result.resolved.candidates),
                "selection_cache" if result.cache_hit else "explored",
            )
            return _no_store_redirect(
                build_manifest_url(request, page_url, selection.revision)
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

    if source == "hls":
        try:
            result = await playback.resolve_hls(resolver, page_url)
        except PlaybackUnavailable as exc:
            logger.warning(
                "event=play_failed page=%s source=hls detail=%s",
                safe_url_for_log(page_url),
                exc,
            )
            _raise_playback_unavailable(exc)
        selection = result.selection
        if request.method == "GET":
            repository.mark_series_watched(page_url)
        assert selection.line is not None
        assert selection.revision is not None
        logger.info(
            "event=play_selected pid=%d mode=hls-explicit line=%d route=%s "
            "line_count=%d strategy=%s",
            result.resolved.pid,
            selection.line,
            selection.route_name,
            len(result.resolved.candidates),
            "selection_cache" if result.cache_hit else "server_selected",
        )
        return _no_store_redirect(
            build_manifest_url(request, page_url, selection.revision)
        )

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
        if request.method == "GET":
            repository.mark_series_watched(page_url)
        return _no_store_redirect(media_url)

    raise AssertionError(f"Unexpected playback source: {source}")


@router.get("/hls.m3u8", name="hls_manifest")
async def hls_manifest(
    request: Request,
    resolver: ResolverDep,
    settings: SettingsDep,
    playback: PlaybackCoordinatorDep,
    segment_cache: SegmentCacheDep,
    page_url: str = Query(...),
    v: str | None = Query(None, max_length=64),
) -> Response:
    try:
        result = await playback.resolve_hls(resolver, page_url)
    except PlaybackUnavailable as exc:
        logger.warning(
            "event=hls_selection_failed page=%s cached=%s detail=%s",
            safe_url_for_log(page_url),
            exc.cached,
            exc,
        )
        _raise_playback_unavailable(exc)

    selection = result.selection
    assert selection.line is not None
    assert selection.revision is not None
    line = selection.line
    resolved = result.resolved
    if v != selection.revision:
        logger.info(
            "event=hls_version_redirected pid=%d line=%d route=%s "
            "requested=%s current=%s",
            resolved.pid,
            line,
            selection.route_name,
            v,
            selection.revision,
        )
        return _no_store_redirect(
            build_manifest_url(request, page_url, selection.revision)
        )
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
    playlist_id: str | None = None
    if settings.proxy_segments:
        segment_endpoint = request.url_for("proxy_segment")
        playlist_id = segment_cache.register_playlist(
            page_url,
            line,
            manifest,
            selection.revision,
        )
        segment_index = 0

        def segment_builder(segment_url: str) -> str:
            nonlocal segment_index
            parameters: dict[str, str | int] = {
                "url": segment_url,
                "referer": page_url,
                "v": selection.revision,
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
        "event=hls_manifest_served pid=%s line=%d route=%s playlist=%s "
        "proxy_segments=%s bytes=%d",
        getattr(_resolved, "pid", "unknown"),
        line,
        selection.route_name,
        playlist_id,
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
) -> PlainTextResponse:
    play_url = build_play_url(request, page_url, source)
    logger.debug(
        "event=strm_generated page=%s source=%s target=%s",
        safe_url_for_log(page_url),
        source,
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
    v: str | None = Query(None, max_length=64),
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

    current = segment_cache.current_segment(referer, index)
    if current is not None and (
        playlist != current.playlist_id
        or v != current.revision
        or url != current.url
    ):
        target = request.url_for("proxy_segment").include_query_params(
            url=current.url,
            referer=current.referer,
            playlist=current.playlist_id,
            index=current.index,
            v=current.revision,
        )
        logger.info(
            "event=segment_version_redirected old_playlist=%s "
            "current_playlist=%s index=%d requested_revision=%s "
            "current_revision=%s",
            playlist,
            current.playlist_id,
            current.index,
            v,
            current.revision,
        )
        return _no_store_redirect(str(target))

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
    client_host = request.client.host if request.client is not None else "unknown"
    logger.debug(
        "event=segment_request method=%s client=%s playlist=%s index=%s "
        "target=%s referer_present=%s range=%s accept=%s user_agent=%r",
        request.method,
        client_host,
        playlist,
        index,
        safe_url_for_log(url),
        referer is not None,
        request.headers.get("range"),
        request.headers.get("accept"),
        request.headers.get("user-agent"),
    )
    upstream_request = client.build_request(request.method, url, headers=headers)
    logger.debug(
        "event=segment_upstream_request target=%s referer=%s range=%s "
        "accept=%s user_agent=%r",
        safe_url_for_log(upstream_request.url),
        safe_url_for_log(referer) if referer else None,
        upstream_request.headers.get("range"),
        upstream_request.headers.get("accept"),
        upstream_request.headers.get("user-agent"),
    )
    upstream_started = time.perf_counter()
    try:
        upstream = await client.send(upstream_request, stream=True)
    except BaseException:
        if claim is not None:
            segment_cache.fail(claim)
        raise
    logger.debug(
        "event=segment_upstream_response target=%s final_target=%s status=%d "
        "content_type=%r content_length=%r content_encoding=%r redirects=%s "
        "headers_seconds=%.3f",
        safe_url_for_log(url),
        safe_url_for_log(upstream.request.url),
        upstream.status_code,
        upstream.headers.get("content-type"),
        upstream.headers.get("content-length"),
        upstream.headers.get("content-encoding"),
        ",".join(
            f"{response.status_code}:{safe_url_for_log(response.request.url)}"
            for response in upstream.history
        )
        or "none",
        time.perf_counter() - upstream_started,
    )
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
    revision: str,
) -> str:
    return str(
        request.url_for("hls_manifest").include_query_params(
            page_url=page_url,
            v=revision,
        )
    )


def build_play_url(
    request: Request,
    page_url: str,
    source: str = "auto",
) -> str:
    url = request.url_for("play").include_query_params(page_url=page_url)
    if source != "auto":
        url = url.include_query_params(source=source)
    return str(url)


def _raise_playback_unavailable(exc: PlaybackUnavailable) -> None:
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
