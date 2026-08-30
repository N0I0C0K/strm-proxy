import asyncio
from collections import Counter

import httpx

from strm_proxy.catalog import XlysCatalog, parse_catalog_page


def _cards(
    entries: list[tuple[int, float | None]],
    *,
    same_title: bool = False,
    description_suffix: str = "HD1080P",
) -> str:
    cards = []
    for content_id, rating in entries:
        title = "同名电影" if same_title else f"电影{content_id}"
        rating_html = (
            f'<div class="rating-badge"><svg><path></path></svg>{rating}</div>'
            if rating is not None
            else ""
        )
        cards.append(
            f"""
            <div class="movie-card">
              <a class="card-img" title="2025剧情《{title}》{description_suffix}" href="/juqing/{content_id}.htm">
                <img src="fallback.jpg" data-src="https://img.example/{content_id}.jpg">
                {rating_html}
              </a>
              <div class="card-info">
                <h4>{title}</h4>
                <div class="card-meta"><span>2026-08-28</span></div>
              </div>
            </div>
            """
        )
    return "".join(cards)


def test_parse_catalog_page_reads_rating_and_source_url() -> None:
    entries = parse_catalog_page(
        _cards([(27078, 8.6)]),
        "https://www.xlys02.com",
    )

    assert len(entries) == 1
    assert entries[0].title == "电影27078"
    assert entries[0].year == 2025
    assert entries[0].douban_rating == 8.6
    assert entries[0].cover_url == "https://img.example/27078.jpg"
    assert entries[0].source_updated_on == "2026-08-28"
    assert entries[0].source_url == (
        "https://www.xlys02.com/juqing/27078.htm"
    )


def test_parse_catalog_page_reads_available_and_complete_episode_counts() -> None:
    updating = parse_catalog_page(
        _cards([(27085, 8.6)], description_suffix="更至04集.HD1080P"),
        "https://www.xlys02.com",
    )[0]
    complete = parse_catalog_page(
        _cards([(27086, 8.8)], description_suffix="15集全.HD1080P"),
        "https://www.xlys02.com",
    )[0]

    assert updating.available_episode_count == 4
    assert updating.declared_episode_count is None
    assert complete.available_episode_count == 15
    assert complete.declared_episode_count == 15


def test_discovery_unions_four_years_above_seven_with_recent_fifty() -> None:
    calls: Counter[tuple[int, int, int | None, int]] = Counter()

    def handle(request: httpx.Request) -> httpx.Response:
        media_type = int(request.url.params["type"])
        order = int(request.url.params["order"])
        year_value = request.url.params.get("year")
        year = int(year_value) if year_value is not None else None
        page = (
            1
            if request.url.path == "/s/all"
            else int(request.url.path.rsplit("/", 1)[1])
        )
        calls[(media_type, order, year, page)] += 1
        if order == 0:
            return httpx.Response(
                200,
                text=_cards([(900, 6.5), (901, None), (902, 7.4)]),
            )
        assert year is not None
        return httpx.Response(
            200,
            text=_cards([(year * 10, 8.2), (year * 10 + 1, 7.0)]),
        )

    async def scenario() -> tuple:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            catalog = XlysCatalog(
                client,
                recent_limit=3,
                year_span=4,
                movie_rating_threshold=7.0,
                current_year=2026,
                request_interval_seconds=0,
            )
            first = await catalog.discover_movies()
            second = await catalog.discover_movies()
            assert first is second
            return first

    entries = asyncio.run(scenario())
    assert [entry.xlys_id for entry in entries] == [
        20260,
        20250,
        20240,
        20230,
        900,
        901,
        902,
    ]
    assert all(entry.douban_rating != 7.0 for entry in entries)
    assert calls[(0, 0, None, 1)] == 1
    assert all(calls[(0, 1, year, 1)] == 1 for year in range(2023, 2027))


def test_duplicate_titles_get_stable_id_suffixes() -> None:
    async def scenario() -> tuple:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.params["order"] == "0":
                return httpx.Response(
                    200,
                    text=_cards([(1, None), (2, None)], same_title=True),
                )
            return httpx.Response(200, text="")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await XlysCatalog(
                client,
                recent_limit=2,
                year_span=1,
                current_year=2026,
                request_interval_seconds=0,
            ).discover_movies()

    entries = asyncio.run(scenario())
    assert [entry.dav_filename for entry in entries] == [
        "同名电影 (2025) [xlys-1].strm",
        "同名电影 (2025) [xlys-2].strm",
    ]


def test_series_rating_threshold_is_strictly_above_eight() -> None:
    async def scenario() -> tuple:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.params["order"] == "0":
                return httpx.Response(200, text=_cards([(900, None)]))
            return httpx.Response(
                200,
                text=_cards([(1, 8.1), (2, 8.0)]),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await XlysCatalog(
                client,
                recent_limit=1,
                year_span=1,
                current_year=2025,
                request_interval_seconds=0,
            ).discover_series()

    entries = asyncio.run(scenario())
    assert [entry.xlys_id for entry in entries] == [1, 900]
