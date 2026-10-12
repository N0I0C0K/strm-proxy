from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .cache import TTLCache
from .detail import parse_season_number
from .models import ResolverError


LIBVIO_HOSTS = frozenset({"www.libvio.lat", "libvio.lat"})
SMARTPLAY_API = "https://hd.ticktockwow.com/smartplay-cache/api/webvideo_ty.php"
DECRYPT_SALT = "RY7e48naFXPsLJC"
PLAY_PATH = re.compile(r"^/play/(\d+)-(\d+)-(\d+)\.html$")
DETAIL_PATH = re.compile(r"^/detail/(\d+)\.html$")
MEDIA_HOST_SUFFIXES = ("bytetos.com", "ibytedtos.com", "ibyteimg.com")
MEDIA_HOSTS = frozenset({"zj-302-cdn-play.samtory.cn"})
logger = logging.getLogger(__name__)


class LibvioNoOnlineRoutes(ResolverError):
    """A valid detail page offers downloads, but no online episodes."""


def is_libvio_url(url: str) -> bool:
    try:
        return urlsplit(url).hostname in LIBVIO_HOSTS
    except ValueError:
        return False


def validate_page_url(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
        valid = (
            parsed.scheme == "https" and parsed.hostname in LIBVIO_HOSTS
            and parsed.username is None and parsed.password is None
            and parsed.port in {None, 443} and not parsed.query and not parsed.fragment
            and (DETAIL_PATH.fullmatch(parsed.path) or PLAY_PATH.fullmatch(parsed.path))
        )
    except ValueError:
        valid = False
    if not valid:
        raise ResolverError("Use a LIBVIO HTTPS detail or play page URL")
    return f"https://{parsed.hostname}{parsed.path}"


class DetailParser(HTMLParser):
    """Keep online routes separate from the site's download playlists."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.cover_url: str | None = None
        self.info_rows: list[str] = []
        self.douban_rating: float | None = None
        self._info_text: list[str] | None = None
        self._rating_text: list[str] | None = None
        self.routes: list[dict] = []
        self._heading = ""
        self._capture: str | None = None
        self._text: list[str] = []
        self._playlist = False
        self._play_path: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "img" and "lazyload" in classes and self.cover_url is None:
            self.cover_url = values.get("data-original") or values.get("src")
        if tag == "p" and "data" in classes:
            self._info_text = []
        if tag == "span" and "douban" in classes:
            self._rating_text = []
        if tag == "h1" and "title" in classes:
            self._start_capture("title")
        elif tag == "h3":
            self._start_capture("heading")
        elif tag == "ul" and "stui-content__playlist" in classes:
            self._playlist = True
            self.routes.append({
                "label": self._heading,
                "type": "download" if "下载" in self._heading or "网盘" in self._heading else "online",
                "episodes": [],
            })
        elif tag == "a" and self._playlist:
            path = values.get("href") or ""
            if PLAY_PATH.fullmatch(path):
                self._play_path = path
                self._start_capture("episode")

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._info_text is not None:
            self.info_rows.append("".join(self._info_text).strip())
            self._info_text = None
        if tag == "span" and self._rating_text is not None:
            try:
                rating = float("".join(self._rating_text).strip())
                if 0 <= rating <= 10:
                    self.douban_rating = rating
            except ValueError:
                pass
            self._rating_text = None
        if (tag, self._capture) in {("h1", "title"), ("h3", "heading"), ("a", "episode")}:
            value = "".join(self._text).strip()
            if self._capture == "title":
                self.title = value
            elif self._capture == "heading":
                self._heading = value
            elif self._play_path:
                match = PLAY_PATH.fullmatch(self._play_path)
                assert match is not None
                self.routes[-1]["episodes"].append({
                    "label": value, "play_path": self._play_path,
                    "public_media_id": match[1], "sid": int(match[2]), "nid": int(match[3]),
                })
            self._capture = None
            self._text = []
        if tag == "ul":
            self._playlist = False

    def handle_data(self, data: str) -> None:
        if self._info_text is not None:
            self._info_text.append(data)
        if self._rating_text is not None:
            self._rating_text.append(data)
        if self._capture:
            self._text.append(data)

    def _start_capture(self, target: str) -> None:
        self._capture = target
        self._text = []


@dataclass(frozen=True, slots=True)
class LibvioEpisode:
    source_index: int
    label: str
    play_path: str


@dataclass(frozen=True, slots=True)
class LibvioDetail:
    public_media_id: str
    kind: Literal["movie", "series"]
    title: str
    year: int | None
    season_number: int | None
    cover_url: str | None
    douban_rating: float | None
    declared_episode_count: int | None
    episodes: tuple[LibvioEpisode, ...]
    source_updated_on: str | None
    source_url: str


def parse_libvio_detail(
    html: str, *, source_url: str, kind_hint: Literal["movie", "series"] | None = None,
) -> LibvioDetail:
    source_url = validate_page_url(source_url)
    match = DETAIL_PATH.fullmatch(urlsplit(source_url).path)
    if match is None:
        raise ResolverError("请输入 LIBVIO 的 HTTPS 影片详情页网址")
    parser = DetailParser()
    parser.feed(html)
    parser.close()
    episodes: dict[int, LibvioEpisode] = {}
    for route in parser.routes:
        if route["type"] != "online":
            continue
        for episode in route["episodes"]:
            if episode["public_media_id"] == match[1] and episode["nid"] > 0:
                index = episode["nid"] - 1
                episodes.setdefault(index, LibvioEpisode(index, episode["label"], episode["play_path"]))
    if not parser.title:
        raise ResolverError("LIBVIO 详情页没有影片标题")
    if not episodes:
        raise LibvioNoOnlineRoutes("LIBVIO 详情页没有可添加的在线播放线路")
    info = "\n".join(parser.info_rows)
    year = re.search(r"年份\s*[：:]\s*((?:19|20)\d{2})", info)
    count = re.search(r"总集数\s*[：:]\s*(\d+)", info)
    updated = re.search(r"最后更新\s*[：:]\s*(\d{4}-\d{2}-\d{2})", info)
    declared = int(count[1]) if count and int(count[1]) > 0 else None
    series_type = re.search(r"类型\s*[：:][^\n]*(?:国产剧|港台剧|日韩剧|海外剧|电视剧|连续剧)", info)
    # Movies can have several quality/version entries on one online route.
    # New series may also declare zero until their total is known.
    numbered_episode = any(re.search(r"第\s*\d+\s*集", episode.label) for episode in episodes.values())
    kind = "series" if series_type or numbered_episode or (declared or 0) > 1 or (count is None and max(episodes) > 0) else "movie"
    # The site's movie/series classification is authoritative when discovered
    # from its lists. Some movie versions are even labelled "第1集".
    if kind_hint is not None:
        kind = kind_hint
    cover = urljoin(source_url, parser.cover_url) if parser.cover_url else None
    if cover and urlsplit(cover).scheme not in {"http", "https"}:
        cover = None
    return LibvioDetail(
        public_media_id=match[1], kind=kind, title=parser.title,
        year=int(year[1]) if year else None, season_number=parse_season_number(parser.title),
        cover_url=cover, douban_rating=parser.douban_rating, declared_episode_count=declared,
        episodes=tuple(episodes[index] for index in sorted(episodes)),
        source_updated_on=updated[1] if updated else None, source_url=source_url,
    )


def player_data(html: str) -> dict:
    match = re.search(r"\bvar\s+player_aaaa\s*=\s*", html)
    if not match:
        raise ResolverError("LIBVIO player data was not found")
    value, _ = json.JSONDecoder().raw_decode(html[match.end():])
    if not isinstance(value, dict):
        raise ResolverError("LIBVIO player data is invalid")
    return value


def constant(html: str, name: str):
    match = re.search(rf"\bconst\s+{re.escape(name)}\s*=\s*", html)
    if not match:
        raise ResolverError(f"LIBVIO player is missing {name}")
    value, _ = json.JSONDecoder().raw_decode(html[match.end():])
    return value


def decrypt_url(value: str, timestamp: str) -> str:
    if not isinstance(value, str):
        raise ResolverError("LIBVIO video URL is invalid")
    if value.startswith(("https://", "http://")):
        return value
    digest = hashlib.md5((str(timestamp) + DECRYPT_SALT).encode()).hexdigest()
    decrypted = AES.new(digest[16:].encode(), AES.MODE_CBC, digest[:16].encode()).decrypt(
        base64.b64decode(value, validate=True)
    )
    return unpad(decrypted, AES.block_size).decode("utf-8")


def _validate_media_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
        or parsed.port not in {None, 443}
        or not (host in MEDIA_HOSTS or any(
            host == suffix or host.endswith("." + suffix) for suffix in MEDIA_HOST_SUFFIXES
        ))
    ):
        raise ResolverError("LIBVIO returned an unsupported media CDN URL")
    return url


@dataclass(frozen=True, slots=True)
class LibvioPlayback:
    page_url: str
    play_page_url: str
    title: str
    public_media_id: str
    route_name: str
    episode_number: int
    media_url: str
    lookup_scheme: str


class LibvioResolver:
    """Resolve the observed ArtPlayer MP4 flow without proxying video bytes."""

    def __init__(self, client: httpx.AsyncClient, media_client: httpx.AsyncClient, *, cache_ttl_seconds: float = 60) -> None:
        self.client = client
        self.media_client = media_client
        self.cache: TTLCache[str, LibvioPlayback] = TTLCache(min(cache_ttl_seconds, 60))
        self._lock = asyncio.Lock()

    async def fetch_detail(
        self, page_url: str, *, kind_hint: Literal["movie", "series"] | None = None,
    ) -> LibvioDetail:
        page_url = validate_page_url(page_url)
        if DETAIL_PATH.fullmatch(urlsplit(page_url).path) is None:
            raise ResolverError("请输入 LIBVIO 的 HTTPS 影片详情页网址")
        response = await self.client.get(page_url)
        response.raise_for_status()
        return parse_libvio_detail(response.text, source_url=page_url, kind_hint=kind_hint)

    async def resolve(self, page_url: str) -> LibvioPlayback:
        page_url = validate_page_url(page_url)
        async with self._lock:
            cached = self.cache.get(page_url)
            if cached is not None:
                if await self._probe(cached.media_url):
                    return cached
            parsed = urlsplit(page_url)
            play_match = PLAY_PATH.fullmatch(parsed.path)
            detail_match = DETAIL_PATH.fullmatch(parsed.path)
            media_id = (play_match or detail_match)[1]
            origin = f"https://{parsed.hostname}"
            detail_url = f"{origin}/detail/{media_id}.html"
            response = await self.client.get(detail_url)
            response.raise_for_status()
            detail = DetailParser()
            detail.feed(response.text)
            if not detail.title:
                raise ResolverError("LIBVIO detail page did not contain a title")
            choices = []
            for route in detail.routes:
                if route["type"] != "online":
                    continue
                for episode in route["episodes"]:
                    if episode["public_media_id"] != media_id:
                        continue
                    if play_match and episode["play_path"] != parsed.path:
                        continue
                    if not play_match and episode["nid"] != 1:
                        continue
                    choices.append((route["label"], episode))
            if not choices:
                raise ResolverError("LIBVIO page did not contain a matching online episode")
            errors = []
            for route_name, episode in choices:
                try:
                    result = await self._resolve_episode(
                        page_url, origin, detail.title, route_name, episode
                    )
                    self.cache.set(page_url, result)
                    return result
                except (httpx.HTTPError, ResolverError, ValueError, KeyError, TypeError) as exc:
                    # Signed URLs and encrypted page parameters stay out of logs.
                    errors.append(f"{route_name}: {type(exc).__name__}")
                    logger.info("event=libvio_route_failed route=%s error=%s", route_name, type(exc).__name__)
            raise ResolverError("LIBVIO online routes failed: " + "; ".join(errors))

    async def _resolve_episode(self, page_url: str, origin: str, title: str, route_name: str, episode: dict) -> LibvioPlayback:
        play_url = urljoin(origin, episode["play_path"])
        response = await self.client.get(play_url)
        response.raise_for_status()
        player = player_data(response.text)
        source = str(player["from"])
        if not re.fullmatch(r"[\w-]+", source):
            raise ResolverError("LIBVIO player adapter is invalid")
        adapter = await self.client.get(f"{origin}/static/player/{source}.js")
        adapter.raise_for_status()
        if "/static/player/artplayer/" not in adapter.text:
            raise ResolverError(f"Unsupported LIBVIO player adapter: {source}")
        iframe = await self.client.get(f"{origin}/static/player/artplayer/", params={
            "url": player["url"], "next": "",
        })
        iframe.raise_for_status()
        timestamp = constant(iframe.text, "timestamp")
        lookup_scheme = "embedded"
        if constant(iframe.text, "isSmartPlay"):
            now = int(time.time())
            body = {
                "vkey": constant(iframe.text, "playPageUrl"),
                "code": constant(iframe.text, "secretKeySeed"),
                "t": now, "signature": hashlib.md5(str(now).encode()).hexdigest(),
            }
            headers = {"Origin": origin, "Referer": origin + "/"}
            try:
                response = await self.client.post(SMARTPLAY_API, json=body, headers=headers, follow_redirects=False)
                lookup_scheme = "https"
            except httpx.ConnectError as exc:
                if "WRONG_VERSION_NUMBER" not in str(exc):
                    raise
                # The same public lookup endpoint supports HTTP. Restrict this
                # compatibility path to plaintext-in-TLS failures; certificate
                # verification errors and other URLs must never be downgraded.
                logger.info("event=libvio_lookup_http_compatibility")
                response = await self.client.post(SMARTPLAY_API.replace("https://", "http://", 1),
                                                  json=body, headers=headers, follow_redirects=False)
                lookup_scheme = "http"
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or data.get("code") not in {None, 0, 200} or not data.get("url"):
                raise ResolverError("LIBVIO video lookup did not return a URL")
            values = [data["url"]]
        else:
            values = [quality["url"] for quality in constant(iframe.text, "qualities")]
        for value in values:
            media = decrypt_url(value, timestamp).split("/T/", 1)[0]
            for candidate in media.split("/fallbackurl/"):
                try:
                    candidate = _validate_media_url(candidate.strip())
                except (ResolverError, ValueError):
                    continue
                if await self._probe(candidate):
                    return LibvioPlayback(page_url, play_url, title, episode["public_media_id"],
                                          route_name, episode["nid"], candidate, lookup_scheme)
        raise ResolverError("LIBVIO did not return a readable MP4 stream")

    async def _probe(self, url: str) -> bool:
        try:
            async with self.media_client.stream("GET", url, headers={"Range": "bytes=0-63"}, follow_redirects=False) as response:
                if response.status_code not in {200, 206}:
                    return False
                prefix = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=64):
                    prefix.extend(chunk[:64 - len(prefix)])
                    if len(prefix) >= 64:
                        break
                return b"ftyp" in prefix[:32]
        except httpx.HTTPError:
            return False
