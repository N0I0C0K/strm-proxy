from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote


class ResolverError(RuntimeError):
    """Raised when an upstream page or media response cannot be resolved."""


@dataclass(frozen=True, slots=True)
class StreamCandidate:
    kind: str
    url: str

    @property
    def route_name(self) -> str | None:
        """Return the stable route label carried in the M3U8 URL fragment."""
        _base, separator, fragment = self.url.partition("#")
        if not separator:
            return None
        name = unquote(fragment).strip()
        return name or None


@dataclass(frozen=True, slots=True)
class ResolvedPage:
    page_url: str
    pid: int
    title: str | None
    candidates: tuple[StreamCandidate, ...]
    tos_available: bool = False
    member_token: str | None = None
