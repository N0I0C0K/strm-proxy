from __future__ import annotations

from dataclasses import dataclass


class ResolverError(RuntimeError):
    """Raised when an upstream page or media response cannot be resolved."""


@dataclass(frozen=True, slots=True)
class StreamCandidate:
    kind: str
    url: str


@dataclass(frozen=True, slots=True)
class ResolvedPage:
    page_url: str
    pid: int
    title: str | None
    candidates: tuple[StreamCandidate, ...]
    tos_available: bool = False
    member_token: str | None = None
