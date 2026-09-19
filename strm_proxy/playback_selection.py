from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
import secrets
from typing import Literal
from urllib.parse import urlparse

import httpx

from .database import CacheRepository
from .logging_utils import describe_http_error, safe_url_for_log
from .models import ResolvedPage
from .models import ResolverError
from .xlys import XlysResolver, validate_page_url


PlaybackSource = Literal["hls", "tos", "member", "url3"]

logger = logging.getLogger(__name__)

FAILURE_CACHE_SECONDS = 30
_EPISODE_PATH = re.compile(r"(?:^|/)play/(?P<series_id>\d+)-\d+\.htm$")


@dataclass(frozen=True, slots=True)
class PlaybackSelection:
    source: PlaybackSource
    line: int | None = None
    route_name: str | None = None
    manual_override: bool = False
    candidate_kind: str | None = None
    candidate_url: str | None = None
    revision: str | None = None


@dataclass(frozen=True, slots=True)
class PlaybackFailure:
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlaybackResult:
    resolved: ResolvedPage
    selection: PlaybackSelection
    media_url: str | None
    cache_hit: bool


class PlaybackUnavailable(RuntimeError):
    def __init__(
        self,
        errors: tuple[str, ...],
        *,
        retry_after_seconds: int = FAILURE_CACHE_SECONDS,
        cached: bool = False,
    ) -> None:
        self.errors = errors
        self.retry_after_seconds = retry_after_seconds
        self.cached = cached
        super().__init__("; ".join(errors) or "No playable source is available")


class PlaybackSelectionCache:
    """Remember the last verified auto-play direction for each play page."""

    def __init__(
        self,
        repository: CacheRepository,
        *,
        enabled: bool = True,
    ) -> None:
        self.enabled = enabled
        self._repository = repository

    def get(self, resolved: ResolvedPage) -> PlaybackSelection | None:
        if not self.enabled:
            return None
        series_id = _series_id_from_page_url(resolved.page_url)
        if series_id is not None:
            series_route = self._series_route(series_id)
            if series_route is not None:
                route_name, revision = series_route
                selection = _manual_route_selection(resolved, route_name, revision)
                if selection is not None:
                    return selection
                logger.warning(
                    "event=series_route_unavailable series_id=%d pid=%d route=%s",
                    series_id, resolved.pid, route_name,
                )
        cache_key = _cache_key(resolved.page_url)
        raw_selection = self._repository.get(cache_key)
        if raw_selection is None:
            logger.debug(
                "event=play_selection_cache_miss pid=%d",
                resolved.pid,
            )
            return None
        try:
            selection = _decode_selection(raw_selection)
        except (TypeError, ValueError, json.JSONDecodeError):
            self._repository.delete(cache_key)
            logger.info(
                "event=play_selection_cache_invalidated pid=%d "
                "reason=invalid_value",
                resolved.pid,
            )
            return None

        stored_selection = selection
        selection, invalid_reason = self._validate_selection(selection, resolved)
        if invalid_reason is not None:
            self._repository.delete(cache_key)
            logger.info(
                "event=play_selection_cache_invalidated pid=%d source=%s "
                "line=%s reason=%s",
                resolved.pid,
                selection.source,
                selection.line,
                invalid_reason,
            )
            return None
        route_remapped = selection != stored_selection
        revision_upgraded = (
            selection.source == "hls" and selection.revision is None
        )
        if revision_upgraded:
            selection = replace(selection, revision=_new_revision())
        if selection != stored_selection:
            self._set(resolved.page_url, selection)
        if route_remapped:
            logger.info(
                "event=play_selection_cache_remapped pid=%d route=%s line=%d",
                resolved.pid,
                selection.route_name,
                selection.line,
            )
        if revision_upgraded:
            logger.info(
                "event=play_selection_cache_upgraded pid=%d revision=%s",
                resolved.pid,
                selection.revision,
            )

        logger.info(
            "event=play_selection_cache_hit pid=%d source=%s line=%s",
            resolved.pid,
            selection.source,
            selection.line,
        )
        return selection

    def remember_direct(
        self,
        resolved: ResolvedPage,
        source: Literal["tos", "member"],
    ) -> None:
        if not self.enabled:
            return
        self._set(resolved.page_url, PlaybackSelection(source=source))
        logger.info(
            "event=play_selection_cache_set pid=%d source=%s line=None",
            resolved.pid,
            source,
        )

    def remember_manual_direct(
        self, resolved: ResolvedPage, source: Literal["tos", "url3"], line: int | None = None
    ) -> PlaybackSelection:
        selection = PlaybackSelection(
            source=source,
            line=line,
            route_name="source:tos" if source == "tos" else f"url3:{line}",
            manual_override=True,
        )
        self._set(resolved.page_url, selection)
        return selection

    def remember_hls(
        self,
        resolved: ResolvedPage,
        line: int,
        *,
        manual_override: bool = False,
    ) -> PlaybackSelection:
        candidate = resolved.candidates[line]
        selection = _hls_selection(
            resolved,
            line,
            manual_override=manual_override,
            revision=(
                _new_revision()
                if self.enabled
                else _stable_hls_revision(candidate.kind, candidate.url)
            ),
        )
        if not self.enabled:
            return selection
        self._set(resolved.page_url, selection)
        logger.info(
            "event=play_selection_cache_set pid=%d source=hls line=%d "
            "route=%s manual=%s revision=%s",
            resolved.pid,
            line,
            candidate.route_name,
            manual_override,
            selection.revision,
        )
        return selection

    def clear(self, page_url: str) -> None:
        """Clear both the successful and short-lived failed decision."""
        if not self.enabled:
            return
        self._repository.delete(_cache_key(page_url))
        self._repository.delete(_failure_cache_key(page_url))
        logger.info(
            "event=play_selection_cache_cleared page=%s",
            safe_url_for_log(page_url),
        )

    def remember_series_route(
        self, series_id: int, route_name: str, page_urls: tuple[str, ...]
    ) -> None:
        """Apply one named HLS route to present and future episodes."""
        if not self.enabled:
            return
        self._repository.set(
            _series_route_key(series_id),
            json.dumps(
                {"route_name": route_name, "revision": _new_revision()},
                separators=(",", ":"),
            ),
        )
        for page_url in page_urls:
            self.clear(page_url)
        logger.info(
            "event=series_route_set series_id=%d route=%s episodes=%d",
            series_id, route_name, len(page_urls),
        )

    def clear_series_route(self, series_id: int, page_urls: tuple[str, ...]) -> None:
        if not self.enabled:
            return
        self._repository.delete(_series_route_key(series_id))
        for page_url in page_urls:
            self.clear(page_url)
        logger.info("event=series_route_cleared series_id=%d", series_id)

    def migrate_legacy_series_route(
        self, series_id: int, page_urls: tuple[str, ...]
    ) -> None:
        """Promote an older first-episode manual choice to the whole series."""
        if not self.enabled or not page_urls or self._series_route(series_id) is not None:
            return
        old = self._stored_selection(page_urls[0])
        if old is None or not old.manual_override:
            return
        if old.source == "hls" and old.route_name:
            key = old.route_name
        elif old.source == "tos":
            key = "source:tos"
        else:
            return
        self.remember_series_route(series_id, key, page_urls)
        logger.info("event=series_route_migrated series_id=%d route=%s", series_id, key)

    def _series_route(self, series_id: int) -> tuple[str, str] | None:
        raw = self._repository.get(_series_route_key(series_id))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            name, revision = data["route_name"], data["revision"]
            if not isinstance(name, str) or not name or not isinstance(revision, str) or not revision:
                raise ValueError("Invalid series route")
            return name, revision
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            self._repository.delete(_series_route_key(series_id))
            return None

    def invalidate(
        self,
        resolved: ResolvedPage,
        selection: PlaybackSelection,
        *,
        reason: str,
    ) -> None:
        if not self.enabled:
            return
        current = self._stored_selection(resolved.page_url)
        if current != selection:
            return
        self._repository.delete(_cache_key(resolved.page_url))
        logger.info(
            "event=play_selection_cache_invalidated pid=%d source=%s "
            "line=%s reason=%s",
            resolved.pid,
            selection.source,
            selection.line,
            reason,
        )

    def invalidate_hls(
        self,
        resolved: ResolvedPage,
        line: int,
        *,
        reason: str,
    ) -> None:
        if not self.enabled:
            return
        current = self._stored_selection(resolved.page_url)
        if current is None or current.source != "hls" or current.line != line:
            return
        self._repository.delete(_cache_key(resolved.page_url))
        logger.info(
            "event=play_selection_cache_invalidated pid=%d source=hls line=%d "
            "reason=%s",
            resolved.pid,
            line,
            reason,
        )

    def get_failure(self, page_url: str) -> PlaybackFailure | None:
        if not self.enabled:
            return None
        raw_failure = self._repository.get(_failure_cache_key(page_url))
        if raw_failure is None:
            return None
        try:
            failure = _decode_failure(raw_failure)
        except (TypeError, ValueError, json.JSONDecodeError):
            self._repository.delete(_failure_cache_key(page_url))
            return None
        logger.info("event=play_failure_cache_hit")
        return failure

    def remember_failure(
        self,
        page_url: str,
        errors: tuple[str, ...],
    ) -> None:
        if not self.enabled:
            return
        self._repository.set(
            _failure_cache_key(page_url),
            json.dumps({"errors": errors}, separators=(",", ":")),
            expires_at=(
                datetime.now(timezone.utc)
                + timedelta(seconds=FAILURE_CACHE_SECONDS)
            ),
        )
        logger.info(
            "event=play_failure_cache_set retry_after_seconds=%d",
            FAILURE_CACHE_SECONDS,
        )

    def clear_failure(self, page_url: str) -> None:
        if self.enabled:
            self._repository.delete(_failure_cache_key(page_url))

    def _set(self, page_url: str, selection: PlaybackSelection) -> None:
        self._repository.set(
            _cache_key(page_url),
            json.dumps(
                {
                    "source": selection.source,
                    "line": selection.line,
                    "route_name": selection.route_name,
                    "manual_override": selection.manual_override,
                    "candidate_kind": selection.candidate_kind,
                    "candidate_url": selection.candidate_url,
                    "revision": selection.revision,
                },
                separators=(",", ":"),
            ),
        )
        self.clear_failure(page_url)

    def _stored_selection(self, page_url: str) -> PlaybackSelection | None:
        raw_selection = self._repository.get(_cache_key(page_url))
        if raw_selection is None:
            return None
        try:
            return _decode_selection(raw_selection)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _validate_selection(
        selection: PlaybackSelection,
        resolved: ResolvedPage,
    ) -> tuple[PlaybackSelection, str | None]:
        if selection.source == "tos":
            return (
                selection,
                None if resolved.tos_available else "source_not_advertised",
            )
        if selection.source == "member":
            return (
                selection,
                (
                    None
                    if resolved.member_token is not None
                    else "source_not_advertised"
                ),
            )
        if selection.source == "url3":
            return (
                selection,
                None if selection.line is not None and 0 <= selection.line < len(resolved.direct_candidates)
                else "direct_line_not_available",
            )
        if selection.route_name is not None:
            wanted_route = selection.route_name.casefold()
            matches = [
                (index, candidate)
                for index, candidate in enumerate(resolved.candidates)
                if candidate.route_name is not None
                and candidate.route_name.casefold() == wanted_route
            ]
            if not matches:
                return selection, "route_not_available"
            if len(matches) > 1:
                return selection, "route_name_ambiguous"
            line, candidate = matches[0]
            return (
                PlaybackSelection(
                    source="hls",
                    line=line,
                    route_name=candidate.route_name,
                    manual_override=selection.manual_override,
                    candidate_kind=candidate.kind,
                    candidate_url=candidate.url,
                    revision=selection.revision,
                ),
                None,
            )
        if (
            selection.line is None
            or selection.line < 0
            or selection.line >= len(resolved.candidates)
        ):
            return selection, "line_not_available"
        candidate = resolved.candidates[selection.line]
        if (
            candidate.kind != selection.candidate_kind
            or candidate.url != selection.candidate_url
        ):
            return selection, "candidate_changed"
        if candidate.route_name is not None:
            return (
                PlaybackSelection(
                    source="hls",
                    line=selection.line,
                    route_name=candidate.route_name,
                    manual_override=selection.manual_override,
                    candidate_kind=candidate.kind,
                    candidate_url=candidate.url,
                    revision=selection.revision,
                ),
                None,
            )
        return selection, None


def _cache_key(page_url: str) -> str:
    return f"playback-selection:{page_url}"


def _series_route_key(series_id: int) -> str:
    return f"playback-series-route:{series_id}"


def _manual_route_selection(
    resolved: ResolvedPage, route_name: str, revision: str
) -> PlaybackSelection | None:
    if route_name == "source:tos":
        return (
            PlaybackSelection(source="tos", route_name=route_name, manual_override=True)
            if resolved.tos_available else None
        )
    if route_name.startswith("url3:"):
        try:
            index = int(route_name[5:])
        except ValueError:
            return None
        return (
            PlaybackSelection(
                source="url3", line=index, route_name=route_name, manual_override=True
            )
            if 0 <= index < len(resolved.direct_candidates) else None
        )
    matches = [
        index for index, candidate in enumerate(resolved.candidates)
        if candidate.route_name is not None
        and candidate.route_name.casefold() == route_name.casefold()
    ]
    return (
        _hls_selection(resolved, matches[0], manual_override=True, revision=revision)
        if len(matches) == 1 else None
    )


def _series_id_from_page_url(page_url: str) -> int | None:
    match = _EPISODE_PATH.search(urlparse(page_url).path)
    return int(match.group("series_id")) if match else None


def _failure_cache_key(page_url: str) -> str:
    return f"playback-failure:{page_url}"


def _new_revision() -> str:
    return secrets.token_hex(6)


def _stable_hls_revision(candidate_kind: str, candidate_url: str) -> str:
    value = f"{candidate_kind}\0{candidate_url}".encode()
    return hashlib.sha256(value).hexdigest()[:12]


def _hls_selection(
    resolved: ResolvedPage,
    line: int,
    *,
    manual_override: bool = False,
    revision: str | None = None,
) -> PlaybackSelection:
    candidate = resolved.candidates[line]
    return PlaybackSelection(
        source="hls",
        line=line,
        route_name=candidate.route_name,
        manual_override=manual_override,
        candidate_kind=candidate.kind,
        candidate_url=candidate.url,
        revision=revision
        or _stable_hls_revision(candidate.kind, candidate.url),
    )


def _decode_selection(value: str) -> PlaybackSelection:
    data = json.loads(value)
    if not isinstance(data, dict):
        raise TypeError("Playback selection must be a JSON object")
    source = data.get("source")
    if source not in {"hls", "tos", "member", "url3"}:
        raise ValueError("Playback selection has an invalid source")
    line = data.get("line")
    if line is not None and type(line) is not int:
        raise TypeError("Playback selection line must be an integer")
    candidate_kind = data.get("candidate_kind")
    candidate_url = data.get("candidate_url")
    revision = data.get("revision")
    route_name = data.get("route_name")
    if route_name is not None and not isinstance(route_name, str):
        raise TypeError("Playback route name must be a string")
    manual_override = data.get("manual_override", False)
    if not isinstance(manual_override, bool):
        raise TypeError("Playback manual override flag must be a boolean")
    if candidate_kind is not None and not isinstance(candidate_kind, str):
        raise TypeError("Playback candidate kind must be a string")
    if candidate_url is not None and not isinstance(candidate_url, str):
        raise TypeError("Playback candidate URL must be a string")
    if revision is not None and (
        not isinstance(revision, str) or not revision or len(revision) > 64
    ):
        raise TypeError("Playback revision must be a non-empty short string")
    return PlaybackSelection(
        source=source,
        line=line,
        route_name=route_name,
        manual_override=manual_override,
        candidate_kind=candidate_kind,
        candidate_url=candidate_url,
        revision=revision,
    )


def _decode_failure(value: str) -> PlaybackFailure:
    data = json.loads(value)
    if not isinstance(data, dict):
        raise TypeError("Playback failure must be a JSON object")
    errors = data.get("errors")
    if not isinstance(errors, list) or not all(
        isinstance(error, str) for error in errors
    ):
        raise TypeError("Playback failure errors must be a string list")
    return PlaybackFailure(errors=tuple(errors))


class PlaybackCoordinator:
    """Share one durable auto-play exploration task per video."""

    def __init__(self, cache: PlaybackSelectionCache) -> None:
        self.cache = cache
        self._inflight: dict[str, asyncio.Task[PlaybackResult]] = {}

    async def resolve_auto(
        self,
        resolver: XlysResolver,
        page_url: str,
        *,
        preferred_line: int = 0,
    ) -> PlaybackResult:
        page_url = validate_page_url(page_url, resolver.allowed_hosts)
        if failure := self.cache.get_failure(page_url):
            raise PlaybackUnavailable(failure.errors, cached=True)

        task = self._inflight.get(page_url)
        if task is None:
            task = asyncio.create_task(
                self._resolve_and_cache(
                    resolver,
                    page_url,
                    preferred_line=preferred_line,
                ),
                name=f"playback-exploration:{page_url}",
            )
            self._inflight[page_url] = task
            task.add_done_callback(
                lambda completed, key=page_url: self._finish_task(key, completed)
            )
            logger.info(
                "event=play_exploration_started page=%s",
                safe_url_for_log(page_url),
            )
        else:
            logger.info(
                "event=play_exploration_joined page=%s",
                safe_url_for_log(page_url),
            )
        return await asyncio.shield(task)

    async def resolve_hls(
        self,
        resolver: XlysResolver,
        page_url: str,
    ) -> PlaybackResult:
        """Resolve an HLS route without accepting a client-supplied line ID."""
        page_url = validate_page_url(page_url, resolver.allowed_hosts)
        try:
            resolved = await resolver.resolve_page(page_url)
        except (ResolverError, httpx.HTTPError) as exc:
            raise PlaybackUnavailable((f"page: {_error_detail(exc)}",)) from exc

        cached_selection = self.cache.get(resolved)
        if cached_selection is not None and cached_selection.source == "hls":
            return PlaybackResult(
                resolved=resolved,
                selection=cached_selection,
                media_url=None,
                cache_hit=True,
            )

        if not resolved.candidates:
            raise PlaybackUnavailable(("hls: no HLS source was advertised",))
        try:
            selected_line = await resolver.find_working_hls_line(page_url)
        except (ResolverError, httpx.HTTPError) as exc:
            raise PlaybackUnavailable((f"hls: {_error_detail(exc)}",)) from exc

        # An explicit HLS request must not replace a valid cached direct source.
        # In that uncommon case, use a deterministic revision so redirects are
        # stable without changing the auto-play decision.
        if cached_selection is None:
            selection = self.cache.remember_hls(resolved, selected_line)
        else:
            selection = _hls_selection(resolved, selected_line)
        return PlaybackResult(
            resolved=resolved,
            selection=selection,
            media_url=None,
            cache_hit=False,
        )

    def invalidate_hls(
        self,
        resolved: ResolvedPage,
        line: int,
        *,
        reason: str,
    ) -> None:
        self.cache.invalidate_hls(resolved, line, reason=reason)

    async def _resolve_and_cache(
        self,
        resolver: XlysResolver,
        page_url: str,
        *,
        preferred_line: int,
    ) -> PlaybackResult:
        try:
            resolved = await resolver.resolve_page(page_url)
        except (ResolverError, httpx.HTTPError) as exc:
            errors = (f"page: {_error_detail(exc)}",)
            self.cache.remember_failure(page_url, errors)
            raise PlaybackUnavailable(errors) from exc

        failed_cached_source: str | None = None
        cached_selection = self.cache.get(resolved)
        if cached_selection is not None:
            if cached_selection.source == "hls":
                return PlaybackResult(
                    resolved=resolved,
                    selection=cached_selection,
                    media_url=None,
                    cache_hit=True,
                )
            try:
                if cached_selection.source == "url3":
                    assert cached_selection.line is not None
                    _resolved, media_url = await resolver.resolve_direct_candidate(
                        page_url, cached_selection.line
                    )
                else:
                    _resolved, media_url = await resolver.resolve_direct_media(
                        page_url, cached_selection.source,
                    )
            except (ResolverError, httpx.HTTPError) as exc:
                failed_cached_source = cached_selection.source
                self.cache.invalidate(
                    resolved,
                    cached_selection,
                    reason="source_resolution_failed",
                )
                errors = [f"{cached_selection.source}: {_error_detail(exc)}"]
            else:
                if not cached_selection.manual_override:
                    self.cache.remember_direct(resolved, cached_selection.source)
                return PlaybackResult(
                    resolved=resolved,
                    selection=cached_selection,
                    media_url=media_url,
                    cache_hit=True,
                )
        else:
            errors = []

        direct_sources = tuple(
            source
            for source, available in (
                ("tos", resolved.tos_available),
                ("member", resolved.member_token is not None),
            )
            if available
        )
        for direct_source in direct_sources:
            if direct_source == failed_cached_source:
                continue
            try:
                _resolved, media_url = await resolver.resolve_direct_media(
                    page_url,
                    direct_source,
                )
            except (ResolverError, httpx.HTTPError) as exc:
                errors.append(f"{direct_source}: {_error_detail(exc)}")
                logger.warning(
                    "event=play_direct_fallback pid=%d source=%s detail=%s",
                    resolved.pid,
                    direct_source,
                    _error_detail(exc),
                )
                continue
            self.cache.remember_direct(resolved, direct_source)
            return PlaybackResult(
                resolved=resolved,
                selection=PlaybackSelection(source=direct_source),
                media_url=media_url,
                cache_hit=False,
            )

        if resolved.candidates:
            try:
                selected_line = await resolver.find_working_hls_line(
                    page_url,
                    preferred_line=preferred_line,
                )
            except (ResolverError, httpx.HTTPError) as exc:
                errors.append(f"hls: {_error_detail(exc)}")
            else:
                selection = self.cache.remember_hls(resolved, selected_line)
                return PlaybackResult(
                    resolved=resolved,
                    selection=selection,
                    media_url=None,
                    cache_hit=False,
                )
        else:
            errors.append("hls: no HLS source was advertised")

        failure_errors = tuple(errors) or ("No playable source is available",)
        self.cache.remember_failure(page_url, failure_errors)
        raise PlaybackUnavailable(failure_errors)

    def _finish_task(
        self,
        page_url: str,
        task: asyncio.Task[PlaybackResult],
    ) -> None:
        if self._inflight.get(page_url) is task:
            self._inflight.pop(page_url, None)
        if task.cancelled():
            return
        task.exception()


def _error_detail(exc: ResolverError | httpx.HTTPError) -> str:
    if isinstance(exc, httpx.HTTPError):
        return describe_http_error(exc)
    return str(exc)
