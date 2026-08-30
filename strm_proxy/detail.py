from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlparse

import httpx

from .models import ResolverError


_DETAIL_PATH_PATTERN = re.compile(r"^/([^/?#]+)/(\d+)\.htm$")
_PLAY_PATH_PATTERN = re.compile(
    r"^/(?:[^/?#]+/)?play/(\d+)-(\d+)\.htm$"
)
_TITLE_YEAR_PATTERN = re.compile(r"\s*\(((?:19|20)\d{2})\)\s*$")
_EPISODE_NUMBER_PATTERN = re.compile(r"(\d+)")
_SEASON_PATTERN = re.compile(
    r"(?:第([一二三四五六七八九十百\d]+)季|Season\s*(\d+))",
    re.IGNORECASE,
)
_SERIES_CATEGORIES = {
    "dongman",
    "gangju",
    "guoju",
    "hanju",
    "meiju",
    "riju",
    "taiju",
    "yingju",
}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


@dataclass(frozen=True, slots=True)
class XlysEpisode:
    source_index: int
    label: str
    play_path: str


@dataclass(frozen=True, slots=True)
class XlysDetail:
    xlys_id: int
    kind: Literal["movie", "series"]
    category: str
    title: str
    year: int | None
    season_number: int | None
    cover_url: str | None
    declared_episode_count: int | None
    episodes: tuple[XlysEpisode, ...]
    source_updated_on: str | None
    source_url: str


class _DetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.title = ""
        self.cover_url: str | None = None
        self.info: dict[str, str] = {}
        self.play_links: list[tuple[str, str]] = []
        self._poster_depth: int | None = None
        self._info_depth: int | None = None
        self._info_item: dict[str, str] | None = None
        self._capture: tuple[str, int] | None = None
        self._capture_text: list[str] = []
        self._play_href: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag not in _VOID_TAGS:
            self.depth += 1
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "div" and "movie-poster" in classes:
            self._poster_depth = self.depth
        elif tag == "img" and self._poster_depth is not None and not self.cover_url:
            self.cover_url = (values.get("data-src") or values.get("src") or "").strip() or None

        if tag == "h1" and "movie-title" in classes:
            self._start_capture("title")
        elif tag == "div" and "info-item" in classes:
            self._info_depth = self.depth
            self._info_item = {}
        elif tag == "span" and self._info_item is not None:
            if "info-label" in classes:
                self._start_capture("info-label")
            elif "info-value" in classes:
                self._start_capture("info-value")
        elif tag == "a" and "play-item" in classes:
            self._play_href = (values.get("href") or "").strip()
            self._start_capture("play-label")

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        if self._capture is not None and self._capture[1] == self.depth:
            target = self._capture[0]
            value = "".join(self._capture_text).strip()
            if target == "title":
                self.title = value
            elif target in {"info-label", "info-value"} and self._info_item is not None:
                self._info_item[target] = value
            elif target == "play-label" and self._play_href:
                self.play_links.append((self._play_href, value))
                self._play_href = None
            self._capture = None
            self._capture_text = []

        if tag == "div" and self._info_depth == self.depth:
            if self._info_item:
                label = self._info_item.get("info-label", "").rstrip("：: ")
                value = self._info_item.get("info-value", "")
                if label and value:
                    self.info[label] = value
            self._info_depth = None
            self._info_item = None
        if tag == "div" and self._poster_depth == self.depth:
            self._poster_depth = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture_text.append(data)

    def _start_capture(self, target: str) -> None:
        self._capture = (target, self.depth)
        self._capture_text = []


def parse_xlys_detail(
    html: str,
    *,
    source_url: str,
    source_updated_on: str | None = None,
) -> XlysDetail:
    parsed_url = urlparse(source_url)
    path_match = _DETAIL_PATH_PATTERN.fullmatch(parsed_url.path)
    if path_match is None:
        raise ResolverError("Unsupported xlys detail URL")

    parser = _DetailParser()
    parser.feed(html)
    parser.close()
    if not parser.title or not parser.play_links:
        raise ResolverError("The xlys detail page did not contain playable media")

    year_match = _TITLE_YEAR_PATTERN.search(parser.title)
    year = int(year_match.group(1)) if year_match else None
    title = _TITLE_YEAR_PATTERN.sub("", parser.title).strip()
    category = path_match.group(1).lower()
    declared_episode_count = _first_number(parser.info.get("集数"))
    xlys_id = int(path_match.group(2))
    episodes = tuple(
        episode
        for play_path, label in parser.play_links
        if (episode := _episode_from_link(play_path, label, xlys_id)) is not None
    )
    if not episodes:
        raise ResolverError("The xlys detail page did not contain valid play links")
    is_series = (
        category in _SERIES_CATEGORIES
        or len(episodes) > 1
        or (declared_episode_count or 0) > 1
    )
    return XlysDetail(
        xlys_id=xlys_id,
        kind="series" if is_series else "movie",
        category=category,
        title=title,
        year=year,
        season_number=parse_season_number(title),
        cover_url=parser.cover_url,
        declared_episode_count=declared_episode_count,
        episodes=episodes,
        source_updated_on=source_updated_on,
        source_url=source_url,
    )


async def fetch_xlys_detail(
    client: httpx.AsyncClient,
    url: str,
    *,
    allowed_hosts: tuple[str, ...],
) -> XlysDetail:
    parsed = urlparse(url.strip())
    allowed = {host.casefold() for host in allowed_hosts}
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() not in allowed
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or _DETAIL_PATH_PATTERN.fullmatch(parsed.path) is None
    ):
        raise ResolverError("请输入本站的 HTTPS 影片详情页网址")

    normalized_url = f"https://{parsed.hostname}{parsed.path}"
    for attempt in range(3):
        response = await client.get(
            normalized_url,
            headers={"Referer": f"https://{parsed.hostname}/"},
        )
        if response.status_code != 429 or attempt == 2:
            break
        await asyncio.sleep(1.5 * (attempt + 1))
    response.raise_for_status()
    return parse_xlys_detail(
        response.text,
        source_url=normalized_url,
        source_updated_on=_http_date(response.headers.get("last-modified")),
    )


def _first_number(value: str | None) -> int | None:
    match = _EPISODE_NUMBER_PATTERN.search(value or "")
    return int(match.group(1)) if match else None


def parse_season_number(title: str) -> int | None:
    match = _SEASON_PATTERN.search(title)
    if match is None:
        return None
    value = match.group(1) or match.group(2)
    if value.isdigit():
        return int(value)
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        tens, ones = value.split("十", 1)
        return (digits.get(tens, 1) * 10) + digits.get(ones, 0)
    return digits.get(value)


def _episode_from_link(
    play_path: str,
    label: str,
    xlys_id: int,
) -> XlysEpisode | None:
    match = _PLAY_PATH_PATTERN.fullmatch(play_path)
    if match is None or int(match.group(1)) != xlys_id:
        return None
    source_index = int(match.group(2))
    return XlysEpisode(
        source_index=source_index,
        label=label or f"第{source_index + 1}集",
        play_path=play_path,
    )


def _http_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date().isoformat()
