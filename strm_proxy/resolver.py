"""Backward-compatible imports; new code should use xlys, hls, and models."""

from .hls import decode_wrapped_manifest, find_mpeg_ts_offset, rewrite_manifest
from .models import ResolvedPage, ResolverError, StreamCandidate
from .xlys import (
    XlysResolver,
    create_signature,
    extract_candidates,
    normalize_candidate_url,
    parse_page,
    validate_page_url,
)

__all__ = [
    "ResolvedPage",
    "ResolverError",
    "StreamCandidate",
    "XlysResolver",
    "create_signature",
    "decode_wrapped_manifest",
    "extract_candidates",
    "find_mpeg_ts_offset",
    "normalize_candidate_url",
    "parse_page",
    "rewrite_manifest",
    "validate_page_url",
]
