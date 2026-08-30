import asyncio

import httpx
import pytest

from strm_proxy.detail import fetch_xlys_detail, parse_xlys_detail
from strm_proxy.models import ResolverError


SERIES_HTML = """
<div class="movie-header">
  <div class="movie-poster"><img src="https://img.example/show.jpg"></div>
  <div class="movie-details">
    <h1 class="movie-title">柯蒂斯总统 第一季 (2026)</h1>
    <div class="info-item"><span class="info-label">集数：</span><span class="info-value">10</span></div>
  </div>
</div>
<div class="play-list">
  <a class="play-item" href="/play/27085-0.htm">第1集</a>
  <a class="play-item" href="/play/27085-2.htm">第3集</a>
  <a class="play-item" href="/play/27085-1.htm">第2集</a>
  <a class="play-item" href="/play/27085-3.htm">第4集</a>
</div>
"""


MOVIE_HTML = """
<div class="movie-header">
  <div class="movie-poster"><img src="https://img.example/movie.jpg"></div>
  <h1 class="movie-title">一部电影 (2025)</h1>
</div>
<a class="play-item" href="/play/27100-0.htm">在线播放</a>
"""


def test_parse_series_detail_preserves_episode_indices() -> None:
    detail = parse_xlys_detail(
        SERIES_HTML,
        source_url="https://www.xlys02.com/meiju/27085.htm",
        source_updated_on="2026-08-28",
    )

    assert detail.kind == "series"
    assert detail.title == "柯蒂斯总统 第一季"
    assert detail.year == 2026
    assert detail.season_number == 1
    assert detail.declared_episode_count == 10
    assert detail.cover_url == "https://img.example/show.jpg"
    assert [episode.source_index for episode in detail.episodes] == [0, 2, 1, 3]
    assert detail.episodes[-1].play_path == "/play/27085-3.htm"


def test_parse_series_detail_preserves_legacy_category_play_path() -> None:
    html = SERIES_HTML.replace(
        "/play/27085-0.htm",
        "/guoju/play/27085-0.htm",
    )

    detail = parse_xlys_detail(
        html,
        source_url="https://www.xlys02.com/guoju/27085.htm",
    )

    assert detail.episodes[0].play_path == "/guoju/play/27085-0.htm"


def test_parse_single_play_detail_as_movie() -> None:
    detail = parse_xlys_detail(
        MOVIE_HTML,
        source_url="https://www.xlys02.com/juqing/27100.htm",
    )

    assert detail.kind == "movie"
    assert detail.title == "一部电影"
    assert len(detail.episodes) == 1


def test_fetch_rejects_non_xlys_hosts_without_requesting_them() -> None:
    calls = 0

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=MOVIE_HTML)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ResolverError):
                await fetch_xlys_detail(
                    client,
                    "https://example.com/juqing/27100.htm",
                    allowed_hosts=("www.xlys02.com",),
                )

    asyncio.run(scenario())
    assert calls == 0
