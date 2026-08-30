from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from starlette.background import BackgroundTask

from .dependencies import HttpClientDep, ResolverDep, SettingsDep
from .hls import prepare_mpeg_ts_stream

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/resolve")
async def resolve(resolver: ResolverDep, page_url: str = Query(...)) -> dict:
    resolved = await resolver.resolve_page(page_url)
    return {
        "page_url": resolved.page_url,
        "pid": resolved.pid,
        "title": resolved.title,
        "lines": [
            {"index": index, "kind": candidate.kind, "url": candidate.url}
            for index, candidate in enumerate(resolved.candidates)
        ],
    }


@router.get("/hls.m3u8", name="hls_manifest")
async def hls_manifest(
    request: Request,
    resolver: ResolverDep,
    settings: SettingsDep,
    page_url: str = Query(...),
    line: int = Query(0, ge=0),
) -> Response:
    segment_builder = None
    if settings.proxy_segments:
        segment_endpoint = request.url_for("proxy_segment")

        def segment_builder(segment_url: str) -> str:
            return str(
                segment_endpoint.include_query_params(
                    url=segment_url,
                    referer=page_url,
                )
            )

    _resolved, _candidate, manifest = await resolver.fetch_manifest(
        page_url=page_url,
        line=line,
        segment_url_builder=segment_builder,
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
    line: int = Query(0, ge=0),
) -> PlainTextResponse:
    return PlainTextResponse(build_manifest_url(request, page_url, line) + "\n")


@router.api_route("/segment", methods=["GET", "HEAD"], name="proxy_segment")
async def proxy_segment(
    request: Request,
    client: HttpClientDep,
    settings: SettingsDep,
    url: str = Query(...),
    referer: str | None = Query(None),
) -> Response:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != settings.segment_host:
        raise HTTPException(
            status_code=400,
            detail=f"Only {settings.segment_host} segments are allowed",
        )

    headers = {"Referer": referer} if referer else {}
    upstream_request = client.build_request(request.method, url, headers=headers)
    upstream = await client.send(upstream_request, stream=True)
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
        await upstream.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"Segment upstream returned HTTP {status_code}",
        )

    try:
        first_chunk, iterator = await prepare_mpeg_ts_stream(upstream)
    except Exception:
        await upstream.aclose()
        raise

    async def decoded_stream() -> AsyncIterator[bytes]:
        yield first_chunk
        async for chunk in iterator:
            yield chunk

    return StreamingResponse(
        decoded_stream(),
        status_code=200,
        media_type="video/mp2t",
        headers=response_headers,
        background=BackgroundTask(upstream.aclose),
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
