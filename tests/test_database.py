from datetime import date
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, inspect, text

from strm_proxy.catalog import CatalogEntry
from strm_proxy.database import (
    MediaType,
    MoviePolicy,
    create_media_repository,
)


def _movie(
    xlys_id: int,
    *,
    title: str | None = None,
    cover_url: str | None = None,
    updated_on: str = "2026-08-28",
) -> CatalogEntry:
    name = title or f"电影{xlys_id}"
    return CatalogEntry(
        xlys_id=xlys_id,
        title=name,
        year=2025,
        cover_url=cover_url,
        source_updated_on=updated_on,
        dav_filename=f"{name} (2025).strm",
        source_url=f"https://www.xlys02.com/juqing/{xlys_id}.htm",
    )


def test_repository_imports_movie_into_unified_media_table() -> None:
    repository = create_media_repository(":memory:")
    repository.import_discovered_movies(
        (_movie(27078, cover_url="https://img/1.jpg"),)
    )

    movies = repository.list_visible_movies()
    assert len(movies) == 1
    assert movies[0].xlys_id == 27078
    assert movies[0].media_type == MediaType.MOVIE.value
    assert movies[0].title == "电影27078"
    assert movies[0].year == 2025
    assert movies[0].cover_url == "https://img/1.jpg"
    assert movies[0].source_url == (
        "https://www.xlys02.com/juqing/27078.htm"
    )
    assert movies[0].source_updated_on == date(2026, 8, 28)
    assert movies[0].policy == MoviePolicy.AUTO.value
    assert movies[0].dav_name == "电影27078 (2025).strm"
    repository.close()


def test_unified_tables_contain_the_agreed_business_fields() -> None:
    repository = create_media_repository(":memory:")
    inspector = inspect(repository.engine)
    media_columns = {
        column["name"] for column in inspector.get_columns("media_items")
    }
    episode_columns = {
        column["name"] for column in inspector.get_columns("episodes")
    }

    assert media_columns == {
        "xlys_id",
        "media_type",
        "title",
        "year",
        "season_number",
        "cover_url",
        "douban_rating",
        "source_url",
        "declared_episode_count",
        "source_updated_on",
        "source_modified_at",
        "last_checked_at",
        "policy",
        "dav_name",
    }
    assert episode_columns == {
        "id",
        "media_xlys_id",
        "source_index",
        "label",
        "play_path",
    }
    repository.close()


def test_rediscovery_only_updates_cover_source_url_and_date() -> None:
    repository = create_media_repository(":memory:")
    repository.import_discovered_movies((_movie(1, title="原名"),))
    repository.set_movie_policy(1, MoviePolicy.KEEP)
    repository.import_discovered_movies(
        (
            CatalogEntry(
                xlys_id=1,
                title="站点新名字",
                year=2026,
                cover_url="https://img/new.jpg",
                source_updated_on="2026-08-29",
                dav_filename="站点新名字 (2026).strm",
                source_url="https://www.xlys02.com/juqing/1.htm",
            ),
        )
    )

    movie = repository.list_visible_movies()[0]
    assert movie.title == "原名"
    assert movie.year == 2025
    assert movie.dav_name == "原名 (2025).strm"
    assert movie.policy == MoviePolicy.KEEP.value
    assert movie.cover_url == "https://img/new.jpg"
    assert movie.source_url == "https://www.xlys02.com/juqing/1.htm"
    assert movie.source_updated_on == date(2026, 8, 29)
    repository.close()


def test_hidden_movies_are_not_visible() -> None:
    repository = create_media_repository(":memory:")
    repository.import_discovered_movies((_movie(1), _movie(2)))
    repository.set_movie_policy(1, MoviePolicy.HIDDEN)

    assert [
        movie.xlys_id for movie in repository.list_visible_movies()
    ] == [2]
    repository.close()


def test_discovery_replaces_only_stale_automatic_movies() -> None:
    repository = create_media_repository(":memory:")
    repository.import_discovered_movies((_movie(1), _movie(2)))
    repository.set_movie_policy(1, MoviePolicy.KEEP)

    repository.import_discovered_movies((_movie(3),), replace_auto=True)

    assert {movie.xlys_id for movie in repository.list_movies()} == {1, 3}
    assert repository.get_movie(1).policy == MoviePolicy.KEEP.value
    assert repository.get_movie(2) is None
    repository.close()


def test_series_cards_create_episode_rows_without_detail_requests() -> None:
    repository = create_media_repository(":memory:")
    entry = CatalogEntry(
        xlys_id=27085,
        title="测试剧 第二季",
        year=2026,
        cover_url="https://img/show.jpg",
        source_updated_on="2026-08-29",
        dav_filename="测试剧 第二季 (2026).strm",
        source_url="https://www.xlys02.com/meiju/27085.htm",
        douban_rating=8.4,
        available_episode_count=3,
        declared_episode_count=10,
    )

    repository.import_discovered_series((entry,), ())

    series = repository.get_series(27085)
    assert series is not None
    assert series.season_number == 2
    assert series.douban_rating == 8.4
    assert series.declared_episode_count == 10
    assert [episode.source_index for episode in repository.list_episodes(27085)] == [
        0,
        1,
        2,
    ]
    assert repository.list_episodes(27085)[0].play_path == "/play/27085-0.htm"
    repository.close()


def test_legacy_movies_table_is_migrated_and_removed() -> None:
    database_path = Path("data") / f"legacy-{uuid4().hex}.db"
    try:
        engine = create_engine("sqlite+pysqlite:///" + database_path.as_posix())
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE movies (
                        xlys_id INTEGER PRIMARY KEY,
                        title VARCHAR(255) NOT NULL,
                        year INTEGER,
                        cover_url VARCHAR(2048),
                        source_updated_on DATE,
                        policy VARCHAR(16) NOT NULL,
                        dav_filename VARCHAR(255) NOT NULL UNIQUE
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO movies VALUES (
                        27078,
                        '迁移电影',
                        2025,
                        'https://img/legacy.jpg',
                        '2026-08-28',
                        'keep',
                        '迁移电影 (2025).strm'
                    )
                    """
                )
            )
        engine.dispose()

        repository = create_media_repository(str(database_path))
        assert "movies" not in inspect(repository.engine).get_table_names()
        migrated = repository.get_movie(27078)
        assert migrated is not None
        assert migrated.title == "迁移电影"
        assert migrated.policy == MoviePolicy.KEEP.value
        assert migrated.dav_name == "迁移电影 (2025).strm"
        repository.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(database_path) + suffix).unlink(missing_ok=True)


def test_sqlite_database_persists_media_between_restarts() -> None:
    database_path = Path("data") / f"test-{uuid4().hex}.db"
    try:
        repository = create_media_repository(str(database_path))
        repository.import_discovered_movies((_movie(27078),))
        repository.close()

        reopened = create_media_repository(str(database_path))
        assert [
            movie.xlys_id for movie in reopened.list_visible_movies()
        ] == [27078]
        reopened.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(database_path) + suffix).unlink(missing_ok=True)
