from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Engine,
    ForeignKey,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete as sql_delete,
    event,
    func,
    inspect,
    select,
    text,
    update as sql_update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .catalog import CatalogEntry, safe_media_name
from .detail import XlysDetail, parse_season_number


class Base(DeclarativeBase):
    pass


class MediaType(StrEnum):
    MOVIE = "movie"
    SERIES = "series"


class MoviePolicy(StrEnum):
    AUTO = "auto"
    KEEP = "keep"
    HIDDEN = "hidden"


class MediaItem(Base):
    __tablename__ = "media_items"
    __table_args__ = (
        CheckConstraint(
            "media_type IN ('movie', 'series')",
            name="ck_media_items_type",
        ),
        CheckConstraint(
            "policy IN ('auto', 'keep', 'hidden')",
            name="ck_media_items_policy",
        ),
        UniqueConstraint(
            "media_type",
            "dav_name",
            name="uq_media_items_type_dav_name",
        ),
        Index(
            "idx_media_items_type_policy_updated",
            "media_type",
            "policy",
            "source_updated_on",
        ),
    )

    xlys_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_type: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    year: Mapped[int | None] = mapped_column(Integer)
    season_number: Mapped[int | None] = mapped_column(Integer)
    cover_url: Mapped[str | None] = mapped_column(String(2048))
    douban_rating: Mapped[float | None] = mapped_column(Float)
    source_url: Mapped[str | None] = mapped_column(String(2048))
    declared_episode_count: Mapped[int | None] = mapped_column(Integer)
    source_updated_on: Mapped[date | None] = mapped_column(Date)
    source_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_watched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    policy: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=MoviePolicy.AUTO.value,
    )
    dav_name: Mapped[str] = mapped_column(String(255), nullable=False)


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (
        UniqueConstraint(
            "media_xlys_id",
            "source_index",
            name="uq_episodes_media_source_index",
        ),
        Index("idx_episodes_media_xlys_id", "media_xlys_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_xlys_id: Mapped[int] = mapped_column(
        ForeignKey("media_items.xlys_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_index: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    play_path: Mapped[str] = mapped_column(String(512), nullable=False)


class CacheEntry(Base):
    __tablename__ = "cache_entries"

    key: Mapped[str] = mapped_column(String(2048), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccessCredential(Base):
    __tablename__ = "access_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    salt: Mapped[str] = mapped_column(String(32), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class DavRevisionRecord(Base):
    __tablename__ = "dav_revisions"

    scope: Mapped[str] = mapped_column(String(255), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


@dataclass(frozen=True)
class DavRevision:
    fingerprint: str
    modified_at: datetime

    @property
    def etag(self) -> str:
        return f'"{self.fingerprint}"'


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000
    ).hex()


class CacheRepository:
    """Minimal persistent key/value cache backed by the application database."""

    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    def get(self, key: str) -> str | None:
        with self._sessions.begin() as session:
            entry = session.get(CacheEntry, key)
            if entry is None:
                return None
            if entry.expires_at is not None:
                expires_at = entry.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= datetime.now(timezone.utc):
                    session.delete(entry)
                    return None
            return entry.value

    def set(
        self,
        key: str,
        value: str,
        *,
        expires_at: datetime | None = None,
    ) -> None:
        with self._sessions.begin() as session:
            entry = session.get(CacheEntry, key)
            if entry is None:
                session.add(
                    CacheEntry(
                        key=key,
                        value=value,
                        expires_at=expires_at,
                    )
                )
            else:
                entry.value = value
                entry.expires_at = expires_at

    def delete(self, key: str) -> None:
        with self._sessions.begin() as session:
            session.execute(sql_delete(CacheEntry).where(CacheEntry.key == key))


class MediaRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    def create_schema(self) -> None:
        _upgrade_media_schema(self.engine)
        Base.metadata.create_all(self.engine)
        _migrate_legacy_movies(self.engine)

    def initialize_credentials(self, username: str, password: str) -> None:
        """Use configured credentials only on the first start of this database."""
        with self._sessions.begin() as session:
            if session.get(AccessCredential, 1) is None:
                salt = secrets.token_hex(16)
                session.add(AccessCredential(
                    id=1, username=username, salt=salt,
                    password_hash=_hash_password(password, salt),
                ))

    def credentials_username(self) -> str:
        with self._sessions() as session:
            credential = session.get(AccessCredential, 1)
            if credential is None:
                raise RuntimeError("Access credentials have not been initialized")
            return credential.username

    def verify_credentials(self, username: str, password: str) -> bool:
        with self._sessions() as session:
            credential = session.get(AccessCredential, 1)
            if credential is None:
                return False
            return secrets.compare_digest(username, credential.username) and secrets.compare_digest(
                _hash_password(password, credential.salt), credential.password_hash
            )

    def dav_revisions(
        self,
        scopes: tuple[str, ...],
        *,
        namespace: str = "",
    ) -> dict[str, DavRevision]:
        """Return persistent revisions for the requested DAV collection scopes.

        Supported scopes are ``root``, ``movies``, ``series`` and
        ``series:<xlys_id>``. A revision advances only when the corresponding
        WebDAV-visible tree changes, and survives process restarts.
        """
        requested = set(scopes)
        if not requested:
            return {}
        invalid = {
            scope
            for scope in requested
            if scope not in {"root", "movies", "series"}
            and not re.fullmatch(r"series:\d+", scope)
        }
        if invalid:
            raise ValueError(f"Unsupported DAV revision scopes: {sorted(invalid)}")

        need_movies = bool(requested & {"root", "movies"})
        need_all_series = bool(requested & {"root", "series"})
        requested_series_ids = {
            int(scope.partition(":")[2])
            for scope in requested
            if scope.startswith("series:")
        }

        with self._sessions.begin() as session:
            movie_state: tuple[tuple[int, str], ...] = ()
            if need_movies:
                movie_state = tuple(
                    tuple(row)
                    for row in session.execute(
                        select(MediaItem.xlys_id, MediaItem.dav_name)
                        .where(
                            MediaItem.media_type == MediaType.MOVIE.value,
                            MediaItem.policy != MoviePolicy.HIDDEN.value,
                        )
                        .order_by(MediaItem.xlys_id)
                    ).all()
                )

            series_query = select(
                MediaItem.xlys_id,
                MediaItem.dav_name,
                MediaItem.title,
                MediaItem.season_number,
            ).where(
                MediaItem.media_type == MediaType.SERIES.value,
                MediaItem.policy != MoviePolicy.HIDDEN.value,
            )
            if not need_all_series:
                series_query = series_query.where(
                    MediaItem.xlys_id.in_(requested_series_ids)
                )
            series_rows = tuple(
                session.execute(series_query.order_by(MediaItem.xlys_id)).all()
            )
            series_ids = tuple(row.xlys_id for row in series_rows)
            episode_rows = (
                tuple(
                    session.execute(
                        select(
                            Episode.media_xlys_id,
                            Episode.source_index,
                            Episode.play_path,
                        )
                        .where(Episode.media_xlys_id.in_(series_ids))
                        .order_by(Episode.media_xlys_id, Episode.source_index)
                    ).all()
                )
                if series_ids
                else ()
            )
            episodes_by_series: dict[int, list[tuple[int, str]]] = {}
            for episode in episode_rows:
                episodes_by_series.setdefault(episode.media_xlys_id, []).append(
                    (episode.source_index, episode.play_path)
                )

            series_fingerprints: dict[int, str] = {}
            for item in series_rows:
                series_fingerprints[item.xlys_id] = _dav_fingerprint(
                    namespace,
                    "series-item",
                    (
                        item.xlys_id,
                        item.dav_name,
                        item.title,
                        item.season_number or 1,
                        episodes_by_series.get(item.xlys_id, []),
                    ),
                )

            fingerprints: dict[str, str] = {}
            movie_fingerprint = _dav_fingerprint(
                namespace,
                "movies",
                movie_state,
            )
            series_fingerprint = _dav_fingerprint(
                namespace,
                "series",
                tuple(sorted(series_fingerprints.items())),
            )
            for scope in requested:
                if scope == "movies":
                    fingerprints[scope] = movie_fingerprint
                elif scope == "series":
                    fingerprints[scope] = series_fingerprint
                elif scope == "root":
                    fingerprints[scope] = _dav_fingerprint(
                        namespace,
                        "root",
                        (movie_fingerprint, series_fingerprint),
                    )
                else:
                    xlys_id = int(scope.partition(":")[2])
                    fingerprints[scope] = series_fingerprints.get(
                        xlys_id,
                        _dav_fingerprint(namespace, "missing-series", xlys_id),
                    )

            now = datetime.now(timezone.utc).replace(microsecond=0)
            return {
                scope: _persist_dav_revision(
                    session,
                    scope,
                    fingerprint,
                    now,
                )
                for scope, fingerprint in fingerprints.items()
            }

    def change_credentials(self, current_password: str, username: str, password: str) -> bool:
        with self._sessions.begin() as session:
            credential = session.get(AccessCredential, 1)
            if credential is None or not secrets.compare_digest(
                _hash_password(current_password, credential.salt), credential.password_hash
            ):
                return False
            salt = secrets.token_hex(16)
            credential.username = username
            credential.salt = salt
            credential.password_hash = _hash_password(password, salt)
            return True

    def clear_all_media(self) -> None:
        """Remove the disposable catalog while preserving the schema."""
        with self._sessions.begin() as session:
            session.execute(sql_delete(MediaItem))

    def has_movies(self) -> bool:
        with self._sessions() as session:
            return (
                session.scalar(
                    select(MediaItem.xlys_id)
                    .where(MediaItem.media_type == MediaType.MOVIE.value)
                    .limit(1)
                )
                is not None
            )

    def import_discovered_movies(
        self,
        movies: tuple[CatalogEntry, ...],
        *,
        replace_auto: bool = False,
    ) -> None:
        if not movies:
            return
        with self._sessions.begin() as session:
            discovered_ids = {movie.xlys_id for movie in movies}
            if replace_auto:
                session.execute(
                    sql_delete(MediaItem).where(
                        MediaItem.media_type == MediaType.MOVIE.value,
                        MediaItem.policy == MoviePolicy.AUTO.value,
                        MediaItem.xlys_id.not_in(discovered_ids),
                    )
                )
            for discovered in movies:
                stored = session.get(MediaItem, discovered.xlys_id)
                source_date = _parse_source_date(discovered.source_updated_on)
                if stored is not None:
                    if stored.media_type != MediaType.MOVIE.value:
                        continue
                    if discovered.cover_url:
                        stored.cover_url = discovered.cover_url
                    stored.douban_rating = discovered.douban_rating
                    if discovered.source_url:
                        stored.source_url = discovered.source_url
                    if source_date is not None:
                        stored.source_updated_on = source_date
                    continue

                dav_name = _available_movie_name(
                    session,
                    discovered.dav_filename,
                    discovered.xlys_id,
                )
                session.add(
                    MediaItem(
                        xlys_id=discovered.xlys_id,
                        media_type=MediaType.MOVIE.value,
                        title=discovered.title,
                        year=discovered.year,
                        season_number=None,
                        cover_url=discovered.cover_url,
                        douban_rating=discovered.douban_rating,
                        source_url=discovered.source_url,
                        declared_episode_count=None,
                        source_updated_on=source_date,
                        source_modified_at=None,
                        last_checked_at=None,
                        policy=MoviePolicy.AUTO.value,
                        dav_name=dav_name,
                    )
                )

    def list_visible_movies(self) -> tuple[MediaItem, ...]:
        with self._sessions() as session:
            kept = tuple(
                session.scalars(
                    select(MediaItem)
                    .where(
                        MediaItem.media_type == MediaType.MOVIE.value,
                        MediaItem.policy == MoviePolicy.KEEP.value,
                    )
                    .order_by(MediaItem.title, MediaItem.xlys_id)
                )
            )
            automatic = tuple(
                session.scalars(
                    select(MediaItem)
                    .where(
                        MediaItem.media_type == MediaType.MOVIE.value,
                        MediaItem.policy == MoviePolicy.AUTO.value,
                    )
                    .order_by(
                        MediaItem.source_updated_on.is_(None),
                        MediaItem.douban_rating.is_(None),
                        MediaItem.douban_rating.desc(),
                        MediaItem.source_updated_on.desc(),
                        MediaItem.xlys_id.desc(),
                    )
                )
            )
            return kept + automatic

    def list_movies(self) -> tuple[MediaItem, ...]:
        with self._sessions() as session:
            return tuple(
                session.scalars(
                    select(MediaItem)
                    .where(MediaItem.media_type == MediaType.MOVIE.value)
                    .order_by(
                        MediaItem.source_updated_on.is_(None),
                        MediaItem.source_updated_on.desc(),
                        MediaItem.xlys_id.desc(),
                    )
                )
            )

    def list_media(self) -> tuple[MediaItem, ...]:
        """Return every managed movie and series for the administration UI."""
        with self._sessions() as session:
            return tuple(
                session.scalars(
                    select(MediaItem).order_by(
                        MediaItem.source_updated_on.is_(None),
                        MediaItem.source_updated_on.desc(),
                        MediaItem.xlys_id.desc(),
                    )
                )
            )

    def get_media(self, xlys_id: int) -> MediaItem | None:
        with self._sessions() as session:
            return session.get(MediaItem, xlys_id)

    def episode_counts(self) -> dict[int, int]:
        """Count stored episodes in one query for catalog cards."""
        with self._sessions() as session:
            return dict(session.execute(
                select(Episode.media_xlys_id, func.count(Episode.id))
                .group_by(Episode.media_xlys_id)
            ).all())

    def mark_series_watched(self, page_url: str) -> None:
        """Record successful episode playback for a known series only."""
        parsed = urlparse(page_url)
        match = re.fullmatch(r"/(?:[^/]+/)?play/(\d+)-(\d+)\.htm", parsed.path)
        if match is None:
            return
        xlys_id, source_index = map(int, match.groups())
        with self._sessions.begin() as session:
            known_episode = session.scalar(
                select(Episode.id).join(MediaItem).where(
                    Episode.media_xlys_id == xlys_id,
                    Episode.source_index == source_index,
                    Episode.play_path == parsed.path,
                    MediaItem.media_type == MediaType.SERIES.value,
                )
            )
            if known_episode is not None:
                session.execute(
                    sql_update(MediaItem)
                    .where(MediaItem.xlys_id == xlys_id)
                    .values(last_watched_at=datetime.now(timezone.utc))
                )

    def recently_watched_series(self, *, days: int = 30) -> tuple[MediaItem, ...]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with self._sessions() as session:
            return tuple(
                session.scalars(
                    select(MediaItem)
                    .where(
                        MediaItem.media_type == MediaType.SERIES.value,
                        MediaItem.last_watched_at >= cutoff,
                    )
                    .order_by(MediaItem.last_watched_at.desc(), MediaItem.xlys_id)
                )
            )

    def refresh_media_from_detail(self, detail: XlysDetail) -> int:
        """Update an existing item from its detail page without changing policy or DAV name."""
        with self._sessions.begin() as session:
            media = session.get(MediaItem, detail.xlys_id)
            if media is None or media.media_type != detail.kind:
                raise ValueError("Media no longer matches its detail page")
            media.title = detail.title
            media.year = detail.year
            media.cover_url = detail.cover_url or media.cover_url
            media.source_url = detail.source_url
            media.last_checked_at = datetime.now(timezone.utc)
            if detail.source_updated_on:
                media.source_updated_on = _parse_source_date(detail.source_updated_on)
                media.source_modified_at = _date_at_utc_midnight(detail.source_updated_on)
            if detail.kind != MediaType.SERIES.value:
                return 0
            media.season_number = detail.season_number
            media.declared_episode_count = detail.declared_episode_count
            existing = {
                episode.source_index: episode
                for episode in session.scalars(
                    select(Episode).where(Episode.media_xlys_id == detail.xlys_id)
                )
            }
            added = 0
            discovered = {episode.source_index for episode in detail.episodes}
            for item in detail.episodes:
                episode = existing.get(item.source_index)
                if episode is None:
                    session.add(
                        Episode(
                            media_xlys_id=detail.xlys_id,
                            source_index=item.source_index,
                            label=item.label,
                            play_path=item.play_path,
                        )
                    )
                    added += 1
                else:
                    episode.label = item.label
                    episode.play_path = item.play_path
            for source_index, episode in existing.items():
                if source_index not in discovered:
                    session.delete(episode)
            return added

    def set_media_policy(self, xlys_id: int, policy: MoviePolicy) -> bool:
        with self._sessions.begin() as session:
            media = session.get(MediaItem, xlys_id)
            if media is None:
                return False
            media.policy = policy.value
            return True

    def set_media_policies(
        self,
        xlys_ids: tuple[int, ...],
        policy: MoviePolicy,
    ) -> int:
        if not xlys_ids:
            return 0
        with self._sessions.begin() as session:
            result = session.execute(
                sql_update(MediaItem)
                .where(MediaItem.xlys_id.in_(xlys_ids))
                .values(policy=policy.value)
            )
            return result.rowcount

    def delete_media(self, xlys_ids: tuple[int, ...]) -> int:
        if not xlys_ids:
            return 0
        with self._sessions.begin() as session:
            result = session.execute(
                sql_delete(MediaItem).where(MediaItem.xlys_id.in_(xlys_ids))
            )
            return result.rowcount

    def get_movie(self, xlys_id: int) -> MediaItem | None:
        with self._sessions() as session:
            return session.scalar(
                select(MediaItem).where(
                    MediaItem.xlys_id == xlys_id,
                    MediaItem.media_type == MediaType.MOVIE.value,
                )
            )

    def set_movie_policy(self, xlys_id: int, policy: MoviePolicy) -> bool:
        with self._sessions.begin() as session:
            movie = session.scalar(
                select(MediaItem).where(
                    MediaItem.xlys_id == xlys_id,
                    MediaItem.media_type == MediaType.MOVIE.value,
                )
            )
            if movie is None:
                return False
            movie.policy = policy.value
            return True

    def set_movie_policies(
        self,
        xlys_ids: tuple[int, ...],
        policy: MoviePolicy,
    ) -> int:
        if not xlys_ids:
            return 0
        with self._sessions.begin() as session:
            result = session.execute(
                sql_update(MediaItem)
                .where(
                    MediaItem.xlys_id.in_(xlys_ids),
                    MediaItem.media_type == MediaType.MOVIE.value,
                )
                .values(policy=policy.value)
            )
            return result.rowcount

    def delete_movies(self, xlys_ids: tuple[int, ...]) -> int:
        if not xlys_ids:
            return 0
        with self._sessions.begin() as session:
            result = session.execute(
                sql_delete(MediaItem).where(
                    MediaItem.xlys_id.in_(xlys_ids),
                    MediaItem.media_type == MediaType.MOVIE.value,
                )
            )
            return result.rowcount

    def has_series(self) -> bool:
        with self._sessions() as session:
            return (
                session.scalar(
                    select(MediaItem.xlys_id)
                    .where(MediaItem.media_type == MediaType.SERIES.value)
                    .limit(1)
                )
                is not None
            )

    def import_discovered_series(
        self,
        entries: tuple[CatalogEntry, ...],
        details: tuple[XlysDetail, ...],
        *,
        replace_auto: bool = False,
    ) -> None:
        if not entries:
            return
        detail_by_id = {detail.xlys_id: detail for detail in details}
        active_ids = {entry.xlys_id for entry in entries}
        checked_at = datetime.now(timezone.utc)
        watched_cutoff = checked_at - timedelta(days=30)
        with self._sessions.begin() as session:
            if replace_auto:
                session.execute(
                    sql_delete(MediaItem).where(
                        MediaItem.media_type == MediaType.SERIES.value,
                        MediaItem.policy == MoviePolicy.AUTO.value,
                        MediaItem.xlys_id.not_in(active_ids),
                        (
                            MediaItem.last_watched_at.is_(None)
                            | (MediaItem.last_watched_at < watched_cutoff)
                        ),
                    )
                )
            for entry in entries:
                detail = detail_by_id.get(entry.xlys_id)
                title = detail.title if detail is not None else entry.title
                year = detail.year if detail is not None else entry.year
                season_number = (
                    detail.season_number
                    if detail is not None
                    else parse_season_number(entry.title)
                )
                stored = session.get(MediaItem, entry.xlys_id)
                if stored is not None and stored.media_type != MediaType.SERIES.value:
                    continue
                source_date = _parse_source_date(entry.source_updated_on)
                source_modified_at = _date_at_utc_midnight(
                    detail.source_updated_on if detail is not None else None
                )
                declared_episode_count = (
                    detail.declared_episode_count
                    if detail is not None
                    else entry.declared_episode_count
                )
                if stored is None:
                    dav_name = _available_media_name(
                        session,
                        MediaType.SERIES,
                        safe_media_name(title, year),
                        entry.xlys_id,
                    )
                    stored = MediaItem(
                        xlys_id=entry.xlys_id,
                        media_type=MediaType.SERIES.value,
                        title=title,
                        year=year,
                        season_number=season_number,
                        cover_url=(
                            detail.cover_url if detail is not None else None
                        ) or entry.cover_url,
                        douban_rating=entry.douban_rating,
                        source_url=(
                            detail.source_url if detail is not None else None
                        ) or entry.source_url,
                        declared_episode_count=declared_episode_count,
                        source_updated_on=source_date,
                        source_modified_at=source_modified_at,
                        last_checked_at=checked_at,
                        policy=MoviePolicy.AUTO.value,
                        dav_name=dav_name,
                    )
                    session.add(stored)
                    session.flush()
                else:
                    stored.title = title
                    stored.year = year
                    stored.season_number = season_number
                    stored.cover_url = (
                        detail.cover_url if detail is not None else None
                    ) or entry.cover_url
                    stored.douban_rating = entry.douban_rating
                    stored.source_url = (
                        detail.source_url if detail is not None else None
                    ) or entry.source_url
                    stored.declared_episode_count = declared_episode_count
                    stored.source_updated_on = source_date
                    stored.source_modified_at = source_modified_at
                    stored.last_checked_at = checked_at

                existing = {
                    episode.source_index: episode
                    for episode in session.scalars(
                        select(Episode).where(
                            Episode.media_xlys_id == entry.xlys_id
                        )
                    )
                }
                discovered_episodes = (
                    tuple(
                        (
                            episode.source_index,
                            episode.label,
                            episode.play_path,
                        )
                        for episode in detail.episodes
                    )
                    if detail is not None
                    else tuple(
                        (
                            index,
                            f"第{index + 1}集",
                            f"/play/{entry.xlys_id}-{index}.htm",
                        )
                        for index in range(entry.available_episode_count or 0)
                    )
                )
                for source_index, label, play_path in discovered_episodes:
                    episode = existing.get(source_index)
                    if episode is None:
                        session.add(
                            Episode(
                                media_xlys_id=entry.xlys_id,
                                source_index=source_index,
                                label=label,
                                play_path=play_path,
                            )
                        )
                    else:
                        episode.label = label
                        episode.play_path = play_path

    def list_visible_series(self) -> tuple[MediaItem, ...]:
        with self._sessions() as session:
            return tuple(
                session.scalars(
                    select(MediaItem)
                    .where(
                        MediaItem.media_type == MediaType.SERIES.value,
                        MediaItem.policy != MoviePolicy.HIDDEN.value,
                    )
                    .order_by(
                        MediaItem.douban_rating.is_(None),
                        MediaItem.douban_rating.desc(),
                        MediaItem.title,
                    )
                )
            )

    def get_series(self, xlys_id: int) -> MediaItem | None:
        with self._sessions() as session:
            return session.scalar(
                select(MediaItem).where(
                    MediaItem.xlys_id == xlys_id,
                    MediaItem.media_type == MediaType.SERIES.value,
                )
            )

    def set_series_policy(self, xlys_id: int, policy: MoviePolicy) -> bool:
        with self._sessions.begin() as session:
            series = session.scalar(
                select(MediaItem).where(
                    MediaItem.xlys_id == xlys_id,
                    MediaItem.media_type == MediaType.SERIES.value,
                )
            )
            if series is None:
                return False
            series.policy = policy.value
            return True

    def find_visible_series_by_dav_name(
        self,
        dav_name: str,
    ) -> MediaItem | None:
        with self._sessions() as session:
            return session.scalar(
                select(MediaItem).where(
                    MediaItem.media_type == MediaType.SERIES.value,
                    MediaItem.policy != MoviePolicy.HIDDEN.value,
                    MediaItem.dav_name == dav_name,
                )
            )

    def list_episodes(self, media_xlys_id: int) -> tuple[Episode, ...]:
        with self._sessions() as session:
            return tuple(
                session.scalars(
                    select(Episode)
                    .where(Episode.media_xlys_id == media_xlys_id)
                    .order_by(Episode.source_index)
                )
            )

    def close(self) -> None:
        self.engine.dispose()


def create_media_repository(database_path: str) -> MediaRepository:
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

    repository = MediaRepository(engine)
    repository.create_schema()
    return repository


def _migrate_legacy_movies(engine: Engine) -> None:
    if "movies" not in inspect(engine).get_table_names():
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT OR IGNORE INTO media_items (
                    xlys_id,
                    media_type,
                    title,
                    year,
                    season_number,
                    cover_url,
                    douban_rating,
                    source_url,
                    declared_episode_count,
                    source_updated_on,
                    source_modified_at,
                    last_checked_at,
                    policy,
                    dav_name
                )
                SELECT
                    xlys_id,
                    'movie',
                    title,
                    year,
                    NULL,
                    cover_url,
                    NULL,
                    NULL,
                    NULL,
                    source_updated_on,
                    NULL,
                    NULL,
                    policy,
                    dav_filename
                FROM movies
                """
            )
        )
        missing = connection.scalar(
            text(
                """
                SELECT COUNT(*)
                FROM movies AS legacy
                LEFT JOIN media_items AS current
                    ON current.xlys_id = legacy.xlys_id
                    AND current.media_type = 'movie'
                WHERE current.xlys_id IS NULL
                """
            )
        )
        if missing:
            raise RuntimeError(
                f"Legacy movie migration left {missing} records unmigrated"
            )
        connection.execute(text("DROP TABLE movies"))


def _upgrade_media_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    if "media_items" not in inspector.get_table_names():
        return
    columns = {
        column["name"] for column in inspector.get_columns("media_items")
    }
    if "douban_rating" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE media_items ADD COLUMN douban_rating FLOAT")
            )
    if "last_watched_at" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE media_items ADD COLUMN last_watched_at DATETIME")
            )
    if "episodes" in inspector.get_table_names():
        episode_columns = {
            column["name"] for column in inspector.get_columns("episodes")
        }
        if "play_path" not in episode_columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE episodes "
                        "ADD COLUMN play_path VARCHAR(512)"
                    )
                )
                connection.execute(
                    text(
                        "UPDATE episodes "
                        "SET play_path = '/play/' || media_xlys_id || '-' "
                        "|| source_index || '.htm' "
                        "WHERE play_path IS NULL"
                    )
                )


def _parse_source_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _date_at_utc_midnight(value: str | None) -> datetime | None:
    parsed = _parse_source_date(value or "")
    if parsed is None:
        return None
    return datetime.combine(parsed, time.min, tzinfo=timezone.utc)


def _dav_fingerprint(namespace: str, kind: str, value: object) -> str:
    payload = json.dumps(
        [namespace, kind, value],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _persist_dav_revision(
    session: Session,
    scope: str,
    fingerprint: str,
    now: datetime,
) -> DavRevision:
    stored = session.get(DavRevisionRecord, scope)
    if stored is None:
        session.execute(
            sqlite_insert(DavRevisionRecord)
            .values(
                scope=scope,
                fingerprint=fingerprint,
                modified_at=now,
            )
            .on_conflict_do_nothing(index_elements=[DavRevisionRecord.scope])
        )
        stored = session.get(DavRevisionRecord, scope)
        if stored is None:
            raise RuntimeError(f"Failed to initialize DAV revision: {scope}")
    if stored.fingerprint != fingerprint:
        previous = stored.modified_at
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        stored.fingerprint = fingerprint
        stored.modified_at = max(now, previous + timedelta(seconds=1))

    modified_at = stored.modified_at
    if modified_at.tzinfo is None:
        modified_at = modified_at.replace(tzinfo=timezone.utc)
    return DavRevision(fingerprint=stored.fingerprint, modified_at=modified_at)


def _available_movie_name(
    session: Session,
    desired: str,
    xlys_id: int,
) -> str:
    owner = session.scalar(
        select(MediaItem.xlys_id).where(
            MediaItem.media_type == MediaType.MOVIE.value,
            MediaItem.dav_name == desired,
        )
    )
    if owner is None or owner == xlys_id:
        return desired
    return desired[:-5] + f" [xlys-{xlys_id}].strm"


def _available_media_name(
    session: Session,
    media_type: MediaType,
    desired: str,
    xlys_id: int,
) -> str:
    owner = session.scalar(
        select(MediaItem.xlys_id).where(
            MediaItem.media_type == media_type.value,
            MediaItem.dav_name == desired,
        )
    )
    if owner is None or owner == xlys_id:
        return desired
    return desired + f" [xlys-{xlys_id}]"
