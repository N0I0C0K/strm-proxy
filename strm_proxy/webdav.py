from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import date, datetime, time, timezone
from email.utils import format_datetime, formatdate
from urllib.parse import quote
from xml.etree import ElementTree

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from .config import DavSettings
from .database import Episode, MediaItem
from .dependencies import DavSettingsDep, MediaLibraryDep
from .library import MediaLibrary
from .logging_utils import safe_url_for_log
from .routes import build_play_url

router = APIRouter(include_in_schema=False)
logger = logging.getLogger(__name__)

DAV_METHODS = ["OPTIONS", "PROPFIND", "GET", "HEAD"]
DAV_HEADERS = {
    "DAV": "1",
    "Allow": ", ".join(DAV_METHODS),
    "MS-Author-Via": "DAV",
}
DAV_NAMESPACE = "DAV:"
DAV_LAST_MODIFIED = formatdate(usegmt=True)


@router.api_route("/dav", methods=DAV_METHODS)
@router.api_route("/dav/", methods=DAV_METHODS)
async def webdav_root(request: Request, settings: DavSettingsDep) -> Response:
    logger.debug(
        "event=webdav_request method=%s path=/dav/ depth=%s",
        request.method,
        request.headers.get("depth", "none"),
    )
    if request.method == "OPTIONS":
        return _options()
    _authenticate(request, settings)
    if request.method == "PROPFIND":
        return _propfind_root(
            settings,
            include_children=request.headers.get("depth", "1") != "0",
        )
    if request.method == "HEAD":
        return Response(headers=DAV_HEADERS, media_type="text/plain")
    return PlainTextResponse(
        (
            "Read-only WebDAV collection: "
            f"{settings.catalog_directory}/, {settings.series_directory}/\n"
        ),
        headers=DAV_HEADERS,
    )


@router.api_route("/dav/{file_path:path}", methods=DAV_METHODS)
async def webdav_file(
    request: Request,
    settings: DavSettingsDep,
    library: MediaLibraryDep,
    file_path: str,
) -> Response:
    if request.method == "OPTIONS":
        return _options()
    _authenticate(request, settings)
    if file_path == settings.filename:
        if request.method == "PROPFIND":
            return _propfind_legacy_file(request, settings)
        return _strm_response(request, settings.page_url)

    catalog_path = settings.catalog_directory
    if file_path.rstrip("/") == catalog_path:
        if request.method == "PROPFIND":
            movies = (
                await library.visible_movies()
                if request.headers.get("depth", "1") != "0"
                else ()
            )
            logger.info(
                "event=webdav_list kind=movie count=%d depth=%s",
                len(movies),
                request.headers.get("depth", "1"),
            )
            return _propfind_catalog(request, settings, movies, library)
        if request.method == "HEAD":
            return Response(headers=DAV_HEADERS, media_type="text/plain")
        return PlainTextResponse(
            f"Read-only movie catalog: {len(await library.visible_movies())} files\n",
            headers=DAV_HEADERS,
        )

    series_path = settings.series_directory
    if file_path.rstrip("/") == series_path:
        if request.method == "PROPFIND":
            series = (
                await library.visible_series()
                if request.headers.get("depth", "1") != "0"
                else ()
            )
            logger.info(
                "event=webdav_list kind=series count=%d depth=%s",
                len(series),
                request.headers.get("depth", "1"),
            )
            return _propfind_series_root(settings, series)
        if request.method == "HEAD":
            return Response(headers=DAV_HEADERS, media_type="text/plain")
        return PlainTextResponse(
            f"Read-only series catalog: {len(await library.visible_series())} shows\n",
            headers=DAV_HEADERS,
        )
    if file_path.startswith(f"{series_path}/"):
        return await _series_resource(
            request,
            settings,
            library,
            file_path[len(series_path) + 1 :].strip("/"),
        )

    prefix = f"{catalog_path}/"
    if not file_path.startswith(prefix) or "/" in file_path[len(prefix) :]:
        raise HTTPException(status_code=404, detail="WebDAV resource not found")
    filename = file_path[len(prefix) :]
    movie = await library.find_visible_movie_by_filename(filename)
    if movie is None:
        raise HTTPException(status_code=404, detail="WebDAV resource not found")
    if request.method == "PROPFIND":
        return _propfind_catalog_file(settings, movie, request, library)
    return _strm_response(request, library.movie_play_url(movie))


async def _series_resource(
    request: Request,
    settings: DavSettings,
    library: MediaLibrary,
    relative_path: str,
) -> Response:
    parts = relative_path.split("/") if relative_path else []
    if not parts:
        raise HTTPException(status_code=404, detail="WebDAV resource not found")
    series = await library.find_visible_series_by_name(parts[0])
    if series is None:
        raise HTTPException(status_code=404, detail="WebDAV resource not found")
    season_directory = library.series_season_directory(series)

    if len(parts) == 1:
        if request.method == "PROPFIND":
            return _propfind_series_item(
                settings,
                series,
                season_directory,
                include_child=request.headers.get("depth", "1") != "0",
            )
        if request.method == "HEAD":
            return Response(headers=DAV_HEADERS, media_type="text/plain")
        return PlainTextResponse(
            f"Read-only series: {series.dav_name}\n",
            headers=DAV_HEADERS,
        )

    if parts[1] != season_directory:
        raise HTTPException(status_code=404, detail="WebDAV resource not found")
    episodes = library.episodes(series)
    if len(parts) == 2:
        if request.method == "PROPFIND":
            return _propfind_series_season(
                request,
                settings,
                series,
                season_directory,
                episodes,
                library,
            )
        if request.method == "HEAD":
            return Response(headers=DAV_HEADERS, media_type="text/plain")
        return PlainTextResponse(
            f"Read-only season: {len(episodes)} episodes\n",
            headers=DAV_HEADERS,
        )

    if len(parts) != 3:
        raise HTTPException(status_code=404, detail="WebDAV resource not found")
    episode = library.find_episode_by_filename(series, parts[2])
    if episode is None:
        raise HTTPException(status_code=404, detail="WebDAV resource not found")
    if request.method == "PROPFIND":
        return _propfind_episode_file(
            request,
            settings,
            series,
            season_directory,
            episode,
            library,
        )
    return _strm_response(request, library.episode_play_url(series, episode))


def _strm_response(request: Request, page_url: str) -> Response:
    content = _strm_content_for_page(request, page_url)
    logger.debug(
        "event=webdav_strm method=%s page=%s target=%s",
        request.method,
        safe_url_for_log(page_url),
        safe_url_for_log(content.strip()),
    )
    headers = {
        **DAV_HEADERS,
        "Content-Length": str(len(content.encode("utf-8"))),
        "ETag": _etag(content),
        "Cache-Control": "no-store",
    }
    if request.method == "HEAD":
        return Response(headers=headers, media_type="text/plain; charset=utf-8")
    return Response(
        content=content,
        headers=headers,
        media_type="text/plain; charset=utf-8",
    )


def _authenticate(request: Request, settings: DavSettings) -> None:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            username, password = "", ""
        if secrets.compare_digest(
            username, settings.username
        ) and secrets.compare_digest(password, settings.password):
            return
    raise HTTPException(
        status_code=401,
        detail="WebDAV authentication required",
        headers={"WWW-Authenticate": 'Basic realm="strm-proxy"'},
    )


def _options() -> Response:
    return Response(status_code=200, headers=DAV_HEADERS)


def _propfind_root(
    settings: DavSettings,
    *,
    include_children: bool,
) -> Response:
    ElementTree.register_namespace("D", DAV_NAMESPACE)
    multistatus = ElementTree.Element(f"{{{DAV_NAMESPACE}}}multistatus")
    modified = DAV_LAST_MODIFIED
    _append_response(
        multistatus,
        href="/dav/",
        display_name="strm-proxy",
        modified=modified,
        collection=True,
    )
    if include_children:
        _append_response(
            multistatus,
            href=f"/dav/{quote(settings.catalog_directory, safe='')}/",
            display_name=settings.catalog_directory,
            modified=modified,
            collection=True,
        )
        _append_response(
            multistatus,
            href=f"/dav/{quote(settings.series_directory, safe='')}/",
            display_name=settings.series_directory,
            modified=modified,
            collection=True,
        )

    return _multistatus_response(multistatus)


def _propfind_series_root(
    settings: DavSettings,
    series_items: tuple[MediaItem, ...],
) -> Response:
    multistatus = _multistatus()
    root_href = f"/dav/{quote(settings.series_directory, safe='')}/"
    _append_response(
        multistatus,
        href=root_href,
        display_name=settings.series_directory,
        modified=DAV_LAST_MODIFIED,
        collection=True,
    )
    for series in series_items:
        _append_response(
            multistatus,
            href=root_href + quote(series.dav_name, safe="") + "/",
            display_name=series.dav_name,
            modified=_dav_modified(series.source_updated_on),
            collection=True,
        )
    return _multistatus_response(multistatus)


def _propfind_series_item(
    settings: DavSettings,
    series: MediaItem,
    season_directory: str,
    *,
    include_child: bool,
) -> Response:
    multistatus = _multistatus()
    href = _series_href(settings, series)
    _append_response(
        multistatus,
        href=href,
        display_name=series.dav_name,
        modified=_dav_modified(series.source_updated_on),
        collection=True,
    )
    if include_child:
        _append_response(
            multistatus,
            href=href + quote(season_directory, safe="") + "/",
            display_name=season_directory,
            modified=_dav_modified(series.source_updated_on),
            collection=True,
        )
    return _multistatus_response(multistatus)


def _propfind_series_season(
    request: Request,
    settings: DavSettings,
    series: MediaItem,
    season_directory: str,
    episodes: tuple[Episode, ...],
    library: MediaLibrary,
) -> Response:
    multistatus = _multistatus()
    href = _series_href(settings, series) + quote(season_directory, safe="") + "/"
    _append_response(
        multistatus,
        href=href,
        display_name=season_directory,
        modified=_dav_modified(series.source_updated_on),
        collection=True,
    )
    if request.headers.get("depth", "1") != "0":
        for episode in episodes:
            filename = library.episode_filename(series, episode)
            content = _strm_content_for_page(
                request,
                library.episode_play_url(series, episode),
            )
            _append_response(
                multistatus,
                href=href + quote(filename, safe=""),
                display_name=filename,
                modified=_dav_modified(series.source_updated_on),
                collection=False,
                content_length=len(content.encode("utf-8")),
                etag=_etag(content),
            )
    return _multistatus_response(multistatus)


def _propfind_episode_file(
    request: Request,
    settings: DavSettings,
    series: MediaItem,
    season_directory: str,
    episode: Episode,
    library: MediaLibrary,
) -> Response:
    multistatus = _multistatus()
    filename = library.episode_filename(series, episode)
    content = _strm_content_for_page(
        request,
        library.episode_play_url(series, episode),
    )
    href = (
        _series_href(settings, series)
        + quote(season_directory, safe="")
        + "/"
        + quote(filename, safe="")
    )
    _append_response(
        multistatus,
        href=href,
        display_name=filename,
        modified=_dav_modified(series.source_updated_on),
        collection=False,
        content_length=len(content.encode("utf-8")),
        etag=_etag(content),
    )
    return _multistatus_response(multistatus)


def _series_href(settings: DavSettings, series: MediaItem) -> str:
    return (
        f"/dav/{quote(settings.series_directory, safe='')}/"
        + quote(series.dav_name, safe="")
        + "/"
    )


def _propfind_legacy_file(request: Request, settings: DavSettings) -> Response:
    multistatus = _multistatus()
    content = _strm_content(request, settings)
    _append_response(
        multistatus,
        href=f"/dav/{quote(settings.filename, safe='')}",
        display_name=settings.filename,
        modified=DAV_LAST_MODIFIED,
        collection=False,
        content_length=len(content.encode("utf-8")),
        etag=_etag(content),
    )
    return _multistatus_response(multistatus)


def _propfind_catalog(
    request: Request,
    settings: DavSettings,
    movies: tuple[MediaItem, ...],
    library: MediaLibrary,
) -> Response:
    multistatus = _multistatus()
    directory_href = f"/dav/{quote(settings.catalog_directory, safe='')}/"
    _append_response(
        multistatus,
        href=directory_href,
        display_name=settings.catalog_directory,
        modified=DAV_LAST_MODIFIED,
        collection=True,
    )
    for movie in movies:
        content = _strm_content_for_page(
            request,
            library.movie_play_url(movie),
        )
        _append_response(
            multistatus,
            href=directory_href + quote(movie.dav_name, safe=""),
            display_name=movie.dav_name,
            modified=_dav_modified(movie.source_updated_on),
            collection=False,
            content_length=len(content.encode("utf-8")),
            etag=_etag(content),
        )
    return _multistatus_response(multistatus)


def _propfind_catalog_file(
    settings: DavSettings,
    movie: MediaItem,
    request: Request,
    library: MediaLibrary,
) -> Response:
    multistatus = _multistatus()
    content = _strm_content_for_page(
        request,
        library.movie_play_url(movie),
    )
    href = f"/dav/{quote(settings.catalog_directory, safe='')}/" + quote(
        movie.dav_name, safe=""
    )
    _append_response(
        multistatus,
        href=href,
        display_name=movie.dav_name,
        modified=_dav_modified(movie.source_updated_on),
        collection=False,
        content_length=len(content.encode("utf-8")),
        etag=_etag(content),
    )
    return _multistatus_response(multistatus)


def _multistatus() -> ElementTree.Element:
    ElementTree.register_namespace("D", DAV_NAMESPACE)
    return ElementTree.Element(f"{{{DAV_NAMESPACE}}}multistatus")


def _multistatus_response(multistatus: ElementTree.Element) -> Response:
    body = ElementTree.tostring(multistatus, encoding="utf-8", xml_declaration=True)
    return Response(
        content=body,
        status_code=207,
        media_type="application/xml; charset=utf-8",
        headers=DAV_HEADERS,
    )


def _append_response(
    multistatus: ElementTree.Element,
    *,
    href: str,
    display_name: str,
    modified: str,
    collection: bool,
    content_length: int | None = None,
    etag: str | None = None,
) -> None:
    response = ElementTree.SubElement(multistatus, f"{{{DAV_NAMESPACE}}}response")
    ElementTree.SubElement(response, f"{{{DAV_NAMESPACE}}}href").text = href
    propstat = ElementTree.SubElement(response, f"{{{DAV_NAMESPACE}}}propstat")
    prop = ElementTree.SubElement(propstat, f"{{{DAV_NAMESPACE}}}prop")
    ElementTree.SubElement(prop, f"{{{DAV_NAMESPACE}}}displayname").text = display_name
    resource_type = ElementTree.SubElement(prop, f"{{{DAV_NAMESPACE}}}resourcetype")
    if collection:
        ElementTree.SubElement(resource_type, f"{{{DAV_NAMESPACE}}}collection")
    ElementTree.SubElement(prop, f"{{{DAV_NAMESPACE}}}getlastmodified").text = modified
    if content_length is not None:
        ElementTree.SubElement(prop, f"{{{DAV_NAMESPACE}}}getcontentlength").text = str(
            content_length
        )
        ElementTree.SubElement(prop, f"{{{DAV_NAMESPACE}}}getcontenttype").text = (
            "text/plain; charset=utf-8"
        )
    if etag:
        ElementTree.SubElement(prop, f"{{{DAV_NAMESPACE}}}getetag").text = etag
    ElementTree.SubElement(propstat, f"{{{DAV_NAMESPACE}}}status").text = (
        "HTTP/1.1 200 OK"
    )


def _strm_content(request: Request, settings: DavSettings) -> str:
    return _strm_content_for_page(request, settings.page_url)


def _strm_content_for_page(request: Request, page_url: str) -> str:
    return build_play_url(request, page_url) + "\n"


def _dav_modified(value: date | str | None) -> str:
    if isinstance(value, date):
        parsed = value
    else:
        try:
            parsed = date.fromisoformat(value or "")
        except ValueError:
            return DAV_LAST_MODIFIED
    return format_datetime(
        datetime.combine(parsed, time.min, tzinfo=timezone.utc),
        usegmt=True,
    )


def _etag(content: str) -> str:
    return f'"{hashlib.sha256(content.encode()).hexdigest()}"'
