from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Literal

import httpx

from .database import CacheRepository
from .logging_utils import describe_http_error, safe_url_for_log
from .models import ResolvedPage
from .models import ResolverError
from .xlys import XlysResolver, validate_page_url


PlaybackSource = Literal["hls", "tos", "member"]

logger = logging.getLogger(__name__)

FAILURE_CACHE_SECONDS = 30


@dataclass(frozen=True, slots=True)
class PlaybackSelection:
    source: PlaybackSource
    line: int | None = None
    candidate_kind: str | None = None
    candidate_url: str | None = None


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

        invalid_reason = self._invalid_reason(selection, resolved)
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

    def remember_hls(self, resolved: ResolvedPage, line: int) -> None:
        if not self.enabled:
            return
        candidate = resolved.candidates[line]
        self._set(
            resolved.page_url,
            PlaybackSelection(
                source="hls",
                line=line,
                candidate_kind=candidate.kind,
                candidate_url=candidate.url,
            ),
        )
        logger.info(
            "event=play_selection_cache_set pid=%d source=hls line=%d",
            resolved.pid,
            line,
        )

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
                    "candidate_kind": selection.candidate_kind,
                    "candidate_url": selection.candidate_url,
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
    def _invalid_reason(
        selection: PlaybackSelection,
        resolved: ResolvedPage,
    ) -> str | None:
        if selection.source == "tos":
            return None if resolved.tos_available else "source_not_advertised"
        if selection.source == "member":
            return (
                None
                if resolved.member_token is not None
                else "source_not_advertised"
            )
        if selection.line is None or selection.line >= len(resolved.candidates):
            return "line_not_available"
        candidate = resolved.candidates[selection.line]
        if (
            candidate.kind != selection.candidate_kind
            or candidate.url != selection.candidate_url
        ):
            return "candidate_changed"
        return None


def _cache_key(page_url: str) -> str:
    return f"playback-selection:{page_url}"


def _failure_cache_key(page_url: str) -> str:
    return f"playback-failure:{page_url}"


def _decode_selection(value: str) -> PlaybackSelection:
    data = json.loads(value)
    if not isinstance(data, dict):
        raise TypeError("Playback selection must be a JSON object")
    source = data.get("source")
    if source not in {"hls", "tos", "member"}:
        raise ValueError("Playback selection has an invalid source")
    line = data.get("line")
    if line is not None and type(line) is not int:
        raise TypeError("Playback selection line must be an integer")
    candidate_kind = data.get("candidate_kind")
    candidate_url = data.get("candidate_url")
    if candidate_kind is not None and not isinstance(candidate_kind, str):
        raise TypeError("Playback candidate kind must be a string")
    if candidate_url is not None and not isinstance(candidate_url, str):
        raise TypeError("Playback candidate URL must be a string")
    return PlaybackSelection(
        source=source,
        line=line,
        candidate_kind=candidate_kind,
        candidate_url=candidate_url,
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
                _resolved, media_url = await resolver.resolve_direct_media(
                    page_url,
                    cached_selection.source,
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
                self.cache.remember_hls(resolved, selected_line)
                return PlaybackResult(
                    resolved=resolved,
                    selection=PlaybackSelection(
                        source="hls",
                        line=selected_line,
                        candidate_kind=resolved.candidates[selected_line].kind,
                        candidate_url=resolved.candidates[selected_line].url,
                    ),
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
