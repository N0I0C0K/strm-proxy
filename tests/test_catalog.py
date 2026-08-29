import asyncio
from collections import Counter

import httpx

from strm_proxy.catalog import XlysCatalog, parse_catalog_page


def _cards(start: int, count: int, *, same_title: bool = False) -> str:
    cards = []
    for offset in range(count):
        content_id = start + offset
        title = "同名电影" if same_title else f"电影{content_id}"
        cards.append(
            f"""
            <div class="movie-card">
              <a class="card-img" title="2025剧情《{title}》HD1080P" href="/juqing/{content_id}.htm">
                <img src="fallback.jpg" data-src="https://img.example/{content_id}.jpg">
              </a>
              <div class="card-info">
                <h4>{title}</h4>
                <div class="card-meta"><span>2026-08-28</span></div>
              </div>
            </div>
            """
        )
    return "".join(cards)


def test_parse_catalog_page_builds_stable_play_urls() -> None:
    movies = parse_catalog_page(
        _cards(27078, 1),
        "https://www.xlys02.com",
    )

    assert len(movies) == 1
    assert movies[0].title == "电影27078"
    assert movies[0].year == 2025
    assert movies[0].cover_url == "https://img.example/27078.jpg"
    assert movies[0].source_updated_on == "2026-08-28"
    assert movies[0].dav_filename == "电影27078 (2025).strm"


def test_catalog_fetches_five_pages_and_limits_result_to_100() -> None:
    calls: Counter[str] = Counter()

    def handle(request: httpx.Request) -> httpx.Response:
        calls[request.url.path] += 1
        assert request.url.params["type"] == "0"
        assert request.url.params["order"] == "0"
        page = 1 if request.url.path == "/s/all" else int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, text=_cards(1000 + (page - 1) * 24, 24))

    async def scenario() -> tuple:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            catalog = XlysCatalog(client, limit=100)
            first = await catalog.latest_movies()
            second = await catalog.latest_movies()
            assert first is second
            return first

    movies = asyncio.run(scenario())
    assert len(movies) == 100
    assert movies[0].xlys_id == 1000
    assert movies[-1].xlys_id == 1099
    assert calls == Counter(
        {"/s/all": 1, "/s/all/2": 1, "/s/all/3": 1, "/s/all/4": 1, "/s/all/5": 1}
    )


def test_duplicate_titles_get_stable_id_suffixes() -> None:
    async def scenario() -> tuple:
        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=_cards(1, 2, same_title=True))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await XlysCatalog(client, limit=2).latest_movies()

    movies = asyncio.run(scenario())
    assert [movie.dav_filename for movie in movies] == [
        "同名电影 (2025) [xlys-1].strm",
        "同名电影 (2025) [xlys-2].strm",
    ]
