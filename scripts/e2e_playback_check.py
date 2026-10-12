from __future__ import annotations

import argparse
import json
import time
from urllib.parse import urljoin, urlparse

import httpx


def _read_prefix(response: httpx.Response, limit: int = 64 * 1024) -> bytes:
    prefix = bytearray()
    for chunk in response.iter_bytes():
        prefix.extend(chunk[: limit - len(prefix)])
        if len(prefix) >= limit:
            break
    return bytes(prefix)


def check_playback(
    client: httpx.Client,
    base_url: str,
    page_url: str,
    *,
    source: str = "auto",
) -> dict[str, object]:
    started = time.perf_counter()
    play = client.get(
        f"{base_url}/play",
        params={"page_url": page_url, "source": source},
        follow_redirects=False,
    )
    if play.status_code != 302:
        play.raise_for_status()
        raise AssertionError(f"expected /play 302, got {play.status_code}")

    location = urljoin(base_url, play.headers["location"])
    if urlparse(location).netloc != urlparse(base_url).netloc:
        with client.stream("GET", location) as media:
            media.raise_for_status()
            prefix = _read_prefix(media)
            content_type = media.headers.get("content-type", "")
        if not prefix:
            raise AssertionError("direct media returned no bytes")
        return {
            "page": page_url,
            "requested_source": source,
            "selected": "direct",
            "media_status": media.status_code,
            "content_type": content_type,
            "sample_bytes": len(prefix),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }

    seen: set[str] = set()
    manifest_redirects = 0
    for _ in range(4):
        if location in seen:
            raise AssertionError("manifest redirect loop detected")
        seen.add(location)
        manifest = client.get(location, follow_redirects=False)
        if manifest.status_code not in {301, 302, 307, 308}:
            break
        manifest_redirects += 1
        location = urljoin(location, manifest.headers["location"])
    else:
        raise AssertionError("too many manifest redirects")

    manifest.raise_for_status()
    if "#EXTM3U" not in manifest.text:
        raise AssertionError("HLS response is not an M3U8 manifest")

    repeated = client.get(location, follow_redirects=False)
    if repeated.status_code != 200:
        raise AssertionError(f"cached manifest was unstable: {repeated.status_code}")

    segment_path = next(
        (
            line.strip()
            for line in manifest.text.splitlines()
            if line.strip() and not line.startswith("#")
        ),
        None,
    )
    if segment_path is None:
        raise AssertionError("manifest contains no media segment")
    segment_url = urljoin(location, segment_path)
    with client.stream("GET", segment_url) as segment:
        segment.raise_for_status()
        prefix = _read_prefix(segment)
        content_type = segment.headers.get("content-type", "")
    if not prefix or prefix[0] != 0x47:
        raise AssertionError("proxied segment does not start with MPEG-TS sync byte")

    return {
        "page": page_url,
        "requested_source": source,
        "selected": "hls",
        "manifest_status": manifest.status_code,
        "manifest_redirects": manifest_redirects,
        "repeat_manifest_status": repeated.status_code,
        "segment_status": segment.status_code,
        "content_type": content_type,
        "sample_bytes": len(prefix),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--source", default="auto", choices=("auto", "hls", "tos"))
    parser.add_argument("page_urls", nargs="+")
    args = parser.parse_args()

    with httpx.Client(timeout=httpx.Timeout(120, connect=20)) as client:
        results = [
            check_playback(
                client,
                args.base_url.rstrip("/"),
                page_url,
                source=args.source,
            )
            for page_url in args.page_urls
        ]
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
