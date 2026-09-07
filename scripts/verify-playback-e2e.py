from __future__ import annotations

import argparse
import json
import os
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


def local_url(url: str, service_url: str) -> str:
    target = urlsplit(url)
    service = urlsplit(service_url)
    return urlunsplit(
        (service.scheme, service.netloc, target.path, target.query, target.fragment)
    )


def ts_stats(payload: bytes) -> dict[str, object]:
    packet_count, remainder = divmod(len(payload), 188)
    bad_sync = sum(
        payload[offset] != 0x47 for offset in range(0, packet_count * 188, 188)
    )
    # These signatures live in elementary-stream payloads. They are a small,
    # dependency-free smoke check rather than a replacement for a full demuxer.
    h264 = any(
        marker in payload
        for marker in (b"\x00\x00\x01\x67", b"\x00\x00\x00\x01\x67")
    )
    aac = b"\xff\xf1" in payload or b"\xff\xf9" in payload
    return {
        "bytes": len(payload),
        "packets": packet_count,
        "bad_sync": bad_sync,
        "remainder": remainder,
        "h264": h264,
        "aac": aac,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify WebDAV STRM -> /play -> HLS -> complete TS segments"
    )
    parser.add_argument("--service-url", default="http://127.0.0.1:8787")
    parser.add_argument("--strm-path", required=True)
    parser.add_argument("--segments", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    result: dict[str, object] = {"ok": False}
    dav_user = os.getenv("STRM_PROXY_DAV_USER", "demo")
    dav_password = os.getenv("STRM_PROXY_DAV_PASSWORD", "demo")
    with httpx.Client(
        timeout=args.timeout,
        auth=httpx.BasicAuth(dav_user, dav_password),
    ) as client:
        started = time.perf_counter()
        strm_response = client.get(urljoin(args.service_url, args.strm_path))
        strm_response.raise_for_status()
        strm_url = strm_response.text.strip()
        result["strm"] = {
            "status": strm_response.status_code,
            "seconds": round(time.perf_counter() - started, 3),
            "url": strm_url,
        }

        started = time.perf_counter()
        play_response = client.get(
            local_url(strm_url, args.service_url),
            follow_redirects=False,
        )
        if play_response.status_code not in {301, 302, 303, 307, 308}:
            raise RuntimeError(
                f"/play returned {play_response.status_code}: {play_response.text}"
            )
        manifest_url = urljoin(strm_url, play_response.headers["location"])
        result["play"] = {
            "status": play_response.status_code,
            "seconds": round(time.perf_counter() - started, 3),
            "location": manifest_url,
        }

        started = time.perf_counter()
        manifest_response = client.get(local_url(manifest_url, args.service_url))
        manifest_response.raise_for_status()
        segment_urls = [
            urljoin(manifest_url, line.strip())
            for line in manifest_response.text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not segment_urls:
            raise RuntimeError("The manifest contains no media segment URLs")
        result["manifest"] = {
            "status": manifest_response.status_code,
            "seconds": round(time.perf_counter() - started, 3),
            "bytes": len(manifest_response.content),
            "segments": len(segment_urls),
            "proxied": all("/segment?" in url for url in segment_urls),
        }

        segment_results: list[dict[str, object]] = []
        for index, segment_url in enumerate(segment_urls[: args.segments]):
            started = time.perf_counter()
            first_byte_seconds: float | None = None
            media = bytearray()
            with client.stream(
                "GET", local_url(segment_url, args.service_url)
            ) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    if first_byte_seconds is None:
                        first_byte_seconds = time.perf_counter() - started
                    media.extend(chunk)
            stats = ts_stats(bytes(media))
            stats.update(
                {
                    "index": index,
                    "status": response.status_code,
                    "first_byte_seconds": round(first_byte_seconds or 0.0, 3),
                    "seconds": round(time.perf_counter() - started, 3),
                }
            )
            segment_results.append(stats)
        result["media"] = segment_results

    strict_ts = all(
        item["packets"] > 0
        and item["bad_sync"] == 0
        and item["remainder"] == 0
        for item in segment_results
    )
    codecs = {
        "h264": any(bool(item["h264"]) for item in segment_results),
        "aac": any(bool(item["aac"]) for item in segment_results),
    }
    result["codecs"] = codecs
    result["ok"] = bool(
        result["manifest"]["proxied"]
        and strict_ts
        and codecs["h264"]
        and codecs["aac"]
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
