from datetime import date
from pathlib import Path
from uuid import uuid4

from sqlalchemy import inspect

from strm_proxy.catalog import CatalogMovie
from strm_proxy.database import MoviePolicy, create_movie_repository


def _movie(
    xlys_id: int,
    *,
    title: str | None = None,
    cover_url: str | None = None,
    updated_on: str = "2026-08-28",
) -> CatalogMovie:
    name = title or f"电影{xlys_id}"
    return CatalogMovie(
        xlys_id=xlys_id,
        title=name,
        year=2025,
        cover_url=cover_url,
        source_updated_on=updated_on,
        dav_filename=f"{name} (2025).strm",
    )


def test_repository_imports_minimal_movie_fields() -> None:
    repository = create_movie_repository(":memory:")
    repository.import_discovered((_movie(27078, cover_url="https://img/1.jpg"),))

    movies = repository.list_visible(100)
    assert len(movies) == 1
    assert movies[0].xlys_id == 27078
    assert movies[0].title == "电影27078"
    assert movies[0].year == 2025
    assert movies[0].cover_url == "https://img/1.jpg"
    assert movies[0].source_updated_on == date(2026, 8, 28)
    assert movies[0].policy == MoviePolicy.AUTO.value
    assert movies[0].dav_filename == "电影27078 (2025).strm"
    repository.close()


def test_movie_table_contains_only_the_agreed_business_fields() -> None:
    repository = create_movie_repository(":memory:")
    columns = {
        column["name"] for column in inspect(repository.engine).get_columns("movies")
    }
    assert columns == {
        "xlys_id",
        "title",
        "year",
        "cover_url",
        "source_updated_on",
        "policy",
        "dav_filename",
    }
    repository.close()


def test_rediscovery_only_updates_cover_and_source_date() -> None:
    repository = create_movie_repository(":memory:")
    repository.import_discovered((_movie(1, title="原名"),))
    repository.set_policy(1, MoviePolicy.KEEP)
    repository.import_discovered(
        (
            CatalogMovie(
                xlys_id=1,
                title="站点新名字",
                year=2026,
                cover_url="https://img/new.jpg",
                source_updated_on="2026-08-29",
                dav_filename="站点新名字 (2026).strm",
            ),
        )
    )

    movie = repository.list_visible(100)[0]
    assert movie.title == "原名"
    assert movie.year == 2025
    assert movie.dav_filename == "原名 (2025).strm"
    assert movie.policy == MoviePolicy.KEEP.value
    assert movie.cover_url == "https://img/new.jpg"
    assert movie.source_updated_on == date(2026, 8, 29)
    repository.close()


def test_hidden_movies_are_not_visible() -> None:
    repository = create_movie_repository(":memory:")
    repository.import_discovered((_movie(1), _movie(2)))
    repository.set_policy(1, MoviePolicy.HIDDEN)

    assert [movie.xlys_id for movie in repository.list_visible(100)] == [2]
    repository.close()


def test_sqlite_database_persists_movies_between_restarts() -> None:
    database_path = Path("data") / f"test-{uuid4().hex}.db"
    try:
        repository = create_movie_repository(str(database_path))
        repository.import_discovered((_movie(27078),))
        repository.close()

        reopened = create_movie_repository(str(database_path))
        assert [movie.xlys_id for movie in reopened.list_visible(100)] == [27078]
        reopened.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(database_path) + suffix).unlink(missing_ok=True)
