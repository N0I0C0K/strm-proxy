from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path

from sqlalchemy import (
    CheckConstraint,
    Date,
    Engine,
    Integer,
    String,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool

from .catalog import CatalogMovie


class Base(DeclarativeBase):
    pass


class MoviePolicy(StrEnum):
    AUTO = "auto"
    KEEP = "keep"
    HIDDEN = "hidden"


class Movie(Base):
    __tablename__ = "movies"
    __table_args__ = (
        CheckConstraint(
            "policy IN ('auto', 'keep', 'hidden')",
            name="ck_movies_policy",
        ),
    )

    xlys_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    year: Mapped[int | None] = mapped_column(Integer)
    cover_url: Mapped[str | None] = mapped_column(String(2048))
    source_updated_on: Mapped[date | None] = mapped_column(Date, index=True)
    policy: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=MoviePolicy.AUTO.value,
        index=True,
    )
    dav_filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
    )


class MovieRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def has_movies(self) -> bool:
        with self._sessions() as session:
            return session.scalar(select(Movie.xlys_id).limit(1)) is not None

    def import_discovered(self, movies: tuple[CatalogMovie, ...]) -> None:
        with self._sessions.begin() as session:
            for discovered in movies:
                stored = session.get(Movie, discovered.xlys_id)
                source_date = _parse_source_date(discovered.source_updated_on)
                if stored is not None:
                    if discovered.cover_url:
                        stored.cover_url = discovered.cover_url
                    if source_date is not None:
                        stored.source_updated_on = source_date
                    continue

                filename = _available_filename(
                    session,
                    discovered.dav_filename,
                    discovered.xlys_id,
                )
                session.add(
                    Movie(
                        xlys_id=discovered.xlys_id,
                        title=discovered.title,
                        year=discovered.year,
                        cover_url=discovered.cover_url,
                        source_updated_on=source_date,
                        policy=MoviePolicy.AUTO.value,
                        dav_filename=filename,
                    )
                )

    def list_visible(self, auto_limit: int) -> tuple[Movie, ...]:
        with self._sessions() as session:
            kept = tuple(
                session.scalars(
                    select(Movie)
                    .where(Movie.policy == MoviePolicy.KEEP.value)
                    .order_by(Movie.title, Movie.xlys_id)
                )
            )
            automatic = tuple(
                session.scalars(
                    select(Movie)
                    .where(Movie.policy == MoviePolicy.AUTO.value)
                    .order_by(
                        Movie.source_updated_on.is_(None),
                        Movie.source_updated_on.desc(),
                        Movie.xlys_id.desc(),
                    )
                    .limit(auto_limit)
                )
            )
            return kept + automatic

    def set_policy(self, xlys_id: int, policy: MoviePolicy) -> bool:
        with self._sessions.begin() as session:
            movie = session.get(Movie, xlys_id)
            if movie is None:
                return False
            movie.policy = policy.value
            return True

    def close(self) -> None:
        self.engine.dispose()


def create_movie_repository(database_path: str) -> MovieRepository:
    if database_path == ":memory:":
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        path = Path(database_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            "sqlite+pysqlite:///" + path.as_posix(),
            connect_args={"check_same_thread": False},
        )

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 5000")
        if database_path != ":memory:":
            cursor.execute("PRAGMA journal_mode = WAL")
        cursor.close()

    repository = MovieRepository(engine)
    repository.create_schema()
    return repository


def _parse_source_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _available_filename(
    session: Session,
    desired: str,
    xlys_id: int,
) -> str:
    owner = session.scalar(
        select(Movie.xlys_id).where(Movie.dav_filename == desired)
    )
    if owner is None or owner == xlys_id:
        return desired
    return desired[:-5] + f" [xlys-{xlys_id}].strm"
