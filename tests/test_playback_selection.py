import asyncio
from collections.abc import Iterator

import pytest

from strm_proxy.database import CacheRepository, create_media_repository
from strm_proxy.models import ResolvedPage, StreamCandidate
from strm_proxy.playback_selection import PlaybackCoordinator, PlaybackSelectionCache


PAGE_URL = "https://www.xlys02.com/play/27062-0.htm"


def _resolved_page(
    *,
    candidates: tuple[StreamCandidate, ...] | None = None,
    tos_available: bool = True,
    member_available: bool = True,
) -> ResolvedPage:
    return ResolvedPage(
        page_url=PAGE_URL,
        pid=204486,
        title="痴迷",
        candidates=candidates
        or (
            StreamCandidate(kind="m3u8", url="https://example/line-0.m3u8"),
            StreamCandidate(kind="m3u8_2", url="https://example/line-1.m3u8"),
        ),
        tos_available=tos_available,
        member_token="available" if member_available else None,
    )


@pytest.fixture
def cache() -> Iterator[PlaybackSelectionCache]:
    repository = create_media_repository(":memory:")
    yield PlaybackSelectionCache(CacheRepository(repository.engine))
    repository.close()


def test_cache_remembers_verified_hls_candidate(
    cache: PlaybackSelectionCache,
) -> None:
    resolved = _resolved_page()

    cache.remember_hls(resolved, 1)

    selection = cache.get(resolved)
    assert selection is not None
    assert selection.source == "hls"
    assert selection.line == 1


def test_cache_invalidates_hls_when_candidate_changes(
    cache: PlaybackSelectionCache,
) -> None:
    resolved = _resolved_page()
    cache.remember_hls(resolved, 1)
    changed = _resolved_page(
        candidates=(
            resolved.candidates[0],
            StreamCandidate(
                kind="m3u8_2",
                url="https://example/replacement.m3u8",
            ),
        )
    )

    assert cache.get(changed) is None


def test_disabled_cache_neither_records_nor_returns_selection() -> None:
    repository = create_media_repository(":memory:")
    try:
        cache = PlaybackSelectionCache(
            CacheRepository(repository.engine),
            enabled=False,
        )
        resolved = _resolved_page()

        cache.remember_direct(resolved, "tos")

        assert cache.get(resolved) is None
    finally:
        repository.close()


def test_manifest_failure_invalidates_matching_hls_selection(
    cache: PlaybackSelectionCache,
) -> None:
    resolved = _resolved_page()
    cache.remember_hls(resolved, 1)

    cache.invalidate_hls(resolved, 1, reason="manifest_fetch_failed")

    assert cache.get(resolved) is None


class _BlockingResolver:
    allowed_hosts = ("www.xlys02.com", "xlys02.com")

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.resolve_page_calls = 0
        self.working_hls_line_calls = 0

    async def resolve_page(self, page_url: str) -> ResolvedPage:
        self.resolve_page_calls += 1
        self.started.set()
        await self.release.wait()
        return _resolved_page(tos_available=False, member_available=False)

    async def find_working_hls_line(
        self,
        page_url: str,
        preferred_line: int = 0,
    ) -> int:
        self.working_hls_line_calls += 1
        return 1


def test_coordinator_merges_concurrent_requests_by_video(
    cache: PlaybackSelectionCache,
) -> None:
    async def scenario() -> None:
        resolver = _BlockingResolver()
        coordinator = PlaybackCoordinator(cache)
        first = asyncio.create_task(coordinator.resolve_auto(resolver, PAGE_URL))
        await resolver.started.wait()
        second = asyncio.create_task(coordinator.resolve_auto(resolver, PAGE_URL))
        await asyncio.sleep(0)

        assert resolver.resolve_page_calls == 1
        resolver.release.set()
        first_result, second_result = await asyncio.gather(first, second)

        assert first_result.selection.line == 1
        assert second_result.selection.line == 1
        assert resolver.working_hls_line_calls == 1

    asyncio.run(scenario())


def test_client_cancellation_does_not_cancel_video_exploration(
    cache: PlaybackSelectionCache,
) -> None:
    async def scenario() -> None:
        resolver = _BlockingResolver()
        coordinator = PlaybackCoordinator(cache)
        disconnected = asyncio.create_task(
            coordinator.resolve_auto(resolver, PAGE_URL)
        )
        await resolver.started.wait()
        disconnected.cancel()
        with pytest.raises(asyncio.CancelledError):
            await disconnected

        retry = asyncio.create_task(coordinator.resolve_auto(resolver, PAGE_URL))
        await asyncio.sleep(0)
        resolver.release.set()
        result = await retry

        assert result.selection.line == 1
        assert resolver.resolve_page_calls == 1
        assert resolver.working_hls_line_calls == 1

    asyncio.run(scenario())
