from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import logging
import re
from urllib.parse import urlparse

import httpx

from .hls import iter_aligned_mpeg_ts, prepare_mpeg_ts_stream
from .logging_utils import describe_http_error, safe_url_for_log


logger = logging.getLogger(__name__)

_EXTINF_PATTERN = re.compile(r"^#EXTINF:([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
_MAX_PLAYLISTS = 64
_DEFAULT_SEGMENT_SECONDS = 6.0


@dataclass(frozen=True, slots=True)
class SegmentPayload:
    data: bytes
    cache_control: str | None = None


@dataclass(frozen=True, slots=True)
class SegmentSpec:
    url: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class SegmentClaim:
    key: str
    payload: SegmentPayload | None = None
    waiter: asyncio.Future[SegmentPayload | None] | None = None
    owner: bool = False


@dataclass(frozen=True, slots=True)
class SegmentLocation:
    playlist_id: str
    index: int
    url: str
    referer: str
    revision: str


@dataclass(frozen=True, slots=True)
class _Playlist:
    referer: str
    revision: str
    segments: tuple[SegmentSpec, ...]


class SegmentCache:
    """Bounded in-memory cache plus forward prefetch for proxied HLS TS."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        segment_host: str,
        max_bytes: int,
        prefetch_seconds: float,
        prefetch_concurrency: int = 2,
    ) -> None:
        self.client = client
        self.segment_host = segment_host
        self.max_bytes = max_bytes
        self.prefetch_seconds = prefetch_seconds
        self.enabled = max_bytes > 0
        self._entries: OrderedDict[str, SegmentPayload] = OrderedDict()
        self._total_bytes = 0
        self._inflight: dict[str, asyncio.Future[SegmentPayload | None]] = {}
        self._playlists: OrderedDict[str, _Playlist] = OrderedDict()
        self._active_playlists: dict[str, str] = {}
        self._desired_windows: dict[str, tuple[int, int]] = {}
        self._prefetch_tasks: dict[str, asyncio.Task[None]] = {}
        self._prefetch_semaphore = asyncio.Semaphore(prefetch_concurrency)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def register_playlist(
        self,
        page_url: str,
        line: int,
        manifest: str,
        revision: str,
    ) -> str | None:
        if not self.enabled:
            return None
        segments = _parse_ts_segments(manifest)
        if not segments:
            return None
        identity = hashlib.sha256(
            f"{page_url}\0{line}\0{manifest}".encode()
        ).hexdigest()[:24]
        self._playlists[identity] = _Playlist(
            referer=page_url,
            revision=revision,
            segments=segments,
        )
        self._active_playlists[page_url] = identity
        self._playlists.move_to_end(identity)
        while len(self._playlists) > _MAX_PLAYLISTS:
            expired_id, _playlist = self._playlists.popitem(last=False)
            self._desired_windows.pop(expired_id, None)
            if self._active_playlists.get(_playlist.referer) == expired_id:
                self._active_playlists.pop(_playlist.referer, None)
        logger.debug(
            "event=segment_playlist_registered playlist=%s segments=%d",
            identity,
            len(segments),
        )
        return identity

    def current_segment(
        self,
        page_url: str | None,
        index: int | None,
    ) -> SegmentLocation | None:
        if not self.enabled or page_url is None or index is None:
            return None
        playlist_id = self._active_playlists.get(page_url)
        if playlist_id is None:
            return None
        playlist = self._playlists.get(playlist_id)
        if playlist is None:
            self._active_playlists.pop(page_url, None)
            return None
        if not 0 <= index < len(playlist.segments):
            return None
        self._playlists.move_to_end(playlist_id)
        return SegmentLocation(
            playlist_id=playlist_id,
            index=index,
            url=playlist.segments[index].url,
            referer=playlist.referer,
            revision=playlist.revision,
        )

    def start_prefetch(
        self,
        playlist_id: str | None,
        index: int | None,
        *,
        requested_url: str,
    ) -> None:
        if (
            not self.enabled
            or self.prefetch_seconds <= 0
            or playlist_id is None
            or index is None
        ):
            return
        playlist = self._playlists.get(playlist_id)
        if playlist is None or not 0 <= index < len(playlist.segments):
            return
        if playlist.segments[index].url != requested_url:
            logger.warning(
                "event=segment_prefetch_metadata_mismatch playlist=%s index=%d",
                playlist_id,
                index,
            )
            return
        self._playlists.move_to_end(playlist_id)
        start = index + 1
        end = _prefetch_end(
            playlist.segments,
            start,
            self.prefetch_seconds,
        )
        if start >= end:
            return
        self._desired_windows[playlist_id] = (start, end)
        task = self._prefetch_tasks.get(playlist_id)
        if task is None or task.done():
            task = asyncio.create_task(
                self._run_prefetch_window(playlist_id),
                name=f"segment-prefetch:{playlist_id}",
            )
            self._prefetch_tasks[playlist_id] = task
            task.add_done_callback(
                lambda completed, key=playlist_id: self._finish_prefetch_task(
                    key,
                    completed,
                )
            )
            logger.info(
                "event=segment_prefetch_started playlist=%s start=%d end=%d "
                "target_seconds=%s",
                playlist_id,
                start,
                end,
                self.prefetch_seconds,
            )
        logger.debug(
            "event=segment_prefetch_window playlist=%s current=%d start=%d "
            "end=%d target_seconds=%s",
            playlist_id,
            index,
            start,
            end,
            self.prefetch_seconds,
        )

    def claim(self, url: str, referer: str | None) -> SegmentClaim:
        key = _segment_key(url, referer)
        payload = self._entries.get(key)
        if payload is not None:
            self._entries.move_to_end(key)
            logger.debug(
                "event=segment_cache_hit target=%s bytes=%d",
                safe_url_for_log(url),
                len(payload.data),
            )
            return SegmentClaim(key=key, payload=payload)
        waiter = self._inflight.get(key)
        if waiter is not None:
            logger.debug(
                "event=segment_cache_join target=%s",
                safe_url_for_log(url),
            )
            return SegmentClaim(key=key, waiter=waiter)
        waiter = asyncio.get_running_loop().create_future()
        self._inflight[key] = waiter
        logger.debug(
            "event=segment_cache_miss target=%s",
            safe_url_for_log(url),
        )
        return SegmentClaim(key=key, owner=True)

    def cached(self, url: str, referer: str | None) -> SegmentPayload | None:
        key = _segment_key(url, referer)
        payload = self._entries.get(key)
        if payload is not None:
            self._entries.move_to_end(key)
        return payload

    def complete_foreground(
        self,
        claim: SegmentClaim,
        payload: SegmentPayload | None,
    ) -> None:
        if not claim.owner:
            return
        if payload is not None and len(payload.data) <= self.max_bytes:
            self._store(payload, claim.key, protected=frozenset())
        self._complete_inflight(claim.key, payload)

    def fail(self, claim: SegmentClaim) -> None:
        if claim.owner:
            self._complete_inflight(claim.key, None)

    async def aclose(self) -> None:
        tasks = tuple(self._prefetch_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for waiter in self._inflight.values():
            if not waiter.done():
                waiter.set_result(None)
        self._inflight.clear()

    async def _run_prefetch_window(self, playlist_id: str) -> None:
        protected: set[str] = set()
        cursor: int | None = None
        previous_desired: tuple[int, int] | None = None
        while True:
            playlist = self._playlists.get(playlist_id)
            desired = self._desired_windows.get(playlist_id)
            if playlist is None or desired is None:
                return
            start, end = desired
            if cursor is None or cursor < start:
                cursor = start
            elif previous_desired is not None and start < previous_desired[0]:
                cursor = start
            previous_desired = desired
            if cursor >= end:
                return

            batch_indices = tuple(range(cursor, min(cursor + 2, end)))
            results = await asyncio.gather(
                *(
                    self._prefetch_one(playlist.segments[item], playlist.referer)
                    for item in batch_indices
                ),
                return_exceptions=True,
            )
            cache_full = False
            for item, result in zip(batch_indices, results, strict=True):
                spec = playlist.segments[item]
                if isinstance(result, BaseException):
                    logger.warning(
                        "event=segment_prefetch_failed playlist=%s index=%d "
                        "target=%s detail=%s",
                        playlist_id,
                        item,
                        safe_url_for_log(spec.url),
                        describe_http_error(result)
                        if isinstance(result, httpx.HTTPError)
                        else str(result),
                    )
                    return
                key, payload, already_cached = result
                if already_cached:
                    protected.add(key)
                    continue
                if payload is None or not self._store(
                    payload,
                    key,
                    protected=frozenset(protected),
                ):
                    cache_full = True
                    break
                protected.add(key)
                logger.debug(
                    "event=segment_prefetch_cached playlist=%s index=%d "
                    "bytes=%d cache_bytes=%d",
                    playlist_id,
                    item,
                    len(payload.data),
                    self._total_bytes,
                )
            if cache_full:
                logger.info(
                    "event=segment_prefetch_capacity_reached playlist=%s "
                    "cache_bytes=%d max_bytes=%d",
                    playlist_id,
                    self._total_bytes,
                    self.max_bytes,
                )
                return
            cursor = batch_indices[-1] + 1

    async def _prefetch_one(
        self,
        spec: SegmentSpec,
        referer: str,
    ) -> tuple[str, SegmentPayload | None, bool]:
        claim = self.claim(spec.url, referer)
        if claim.payload is not None:
            return claim.key, claim.payload, True
        if claim.waiter is not None:
            payload = await asyncio.shield(claim.waiter)
            return claim.key, payload, payload is not None
        try:
            async with self._prefetch_semaphore:
                payload = await self._download_segment(spec.url, referer)
        except BaseException:
            self.fail(claim)
            raise
        self._complete_inflight(claim.key, payload)
        return claim.key, payload, False

    async def _download_segment(
        self,
        url: str,
        referer: str,
    ) -> SegmentPayload:
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            raise ValueError(
                "Prefetch segment URL is outside the allowed host"
            ) from None
        if (
            parsed.scheme != "https"
            or parsed.hostname != self.segment_host
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
        ):
            raise ValueError("Prefetch segment URL is outside the allowed host")
        request = self.client.build_request(
            "GET",
            url,
            headers={"Referer": referer},
        )
        response = await self.client.send(request, stream=True)
        try:
            response.raise_for_status()
            first_chunk, iterator = await prepare_mpeg_ts_stream(response)
            data = bytearray()
            async for chunk in iter_aligned_mpeg_ts(
                first_chunk,
                iterator,
                source_url=url,
            ):
                data.extend(chunk)
                if len(data) > self.max_bytes:
                    raise ValueError("One segment exceeds the entire cache capacity")
            return SegmentPayload(
                data=bytes(data),
                cache_control=response.headers.get("cache-control"),
            )
        finally:
            await response.aclose()

    def _store(
        self,
        payload: SegmentPayload,
        key: str,
        *,
        protected: frozenset[str],
    ) -> bool:
        size = len(payload.data)
        if size > self.max_bytes:
            return False
        existing = self._entries.pop(key, None)
        if existing is not None:
            self._total_bytes -= len(existing.data)
        while self._total_bytes + size > self.max_bytes:
            evicted_key = next(
                (
                    candidate
                    for candidate in self._entries
                    if candidate not in protected
                ),
                None,
            )
            if evicted_key is None:
                if existing is not None:
                    self._entries[key] = existing
                    self._total_bytes += len(existing.data)
                return False
            evicted = self._entries.pop(evicted_key)
            self._total_bytes -= len(evicted.data)
            logger.debug(
                "event=segment_cache_evicted bytes=%d cache_bytes=%d",
                len(evicted.data),
                self._total_bytes,
            )
        self._entries[key] = payload
        self._total_bytes += size
        return True

    def _complete_inflight(
        self,
        key: str,
        payload: SegmentPayload | None,
    ) -> None:
        waiter = self._inflight.pop(key, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(payload)

    def _finish_prefetch_task(
        self,
        playlist_id: str,
        task: asyncio.Task[None],
    ) -> None:
        if self._prefetch_tasks.get(playlist_id) is task:
            self._prefetch_tasks.pop(playlist_id, None)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.warning(
                "event=segment_prefetch_task_failed playlist=%s detail=%s",
                playlist_id,
                exception,
            )
        else:
            logger.info(
                "event=segment_prefetch_finished playlist=%s entries=%d "
                "cache_bytes=%d",
                playlist_id,
                len(self._entries),
                self._total_bytes,
            )


def _parse_ts_segments(manifest: str) -> tuple[SegmentSpec, ...]:
    segments: list[SegmentSpec] = []
    duration = _DEFAULT_SEGMENT_SECONDS
    for line in manifest.splitlines():
        stripped = line.strip()
        match = _EXTINF_PATTERN.match(stripped)
        if match:
            duration = float(match.group(1))
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if urlparse(stripped).path.lower().endswith(".ts"):
            segments.append(
                SegmentSpec(url=stripped, duration_seconds=duration)
            )
            duration = _DEFAULT_SEGMENT_SECONDS
    return tuple(segments)


def _prefetch_end(
    segments: tuple[SegmentSpec, ...],
    start: int,
    target_seconds: float,
) -> int:
    duration = 0.0
    end = start
    while end < len(segments) and duration < target_seconds:
        duration += segments[end].duration_seconds
        end += 1
    return end


def _segment_key(url: str, referer: str | None) -> str:
    return hashlib.sha256(f"{url}\0{referer or ''}".encode()).hexdigest()
