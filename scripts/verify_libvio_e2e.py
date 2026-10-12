"""Verify LIBVIO through a temporary local service and real CDN Range requests.

The service uses an in-memory database. --preview keeps the service available
for a browser playback check until Ctrl+C. Reports omit temporary signed URLs.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
import uvicorn
from fastapi.responses import HTMLResponse

from strm_proxy.app import create_app
from strm_proxy.config import AppSettings, DavSettings

DEFAULT_DETAIL_URL = "https://www.libvio.lat/detail/5813847.html"
PROBE_BYTES = 64 * 1024


def probe_range(client: httpx.Client, url: str, byte_range: str) -> dict:
    """Check bounded Range reads without recording temporary signed URLs."""
    started = time.perf_counter()
    with client.stream("GET", url, headers={"Range": byte_range}) as response:
        response.raise_for_status()
        payload = bytearray()
        for chunk in response.iter_bytes(8192):
            payload.extend(chunk[: PROBE_BYTES - len(payload)])
            if len(payload) >= PROBE_BYTES:
                break
        content_range = response.headers.get("content-range", "")
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
        range_valid = match is not None
        if match:
            first, last, size = map(int, match.groups())
            requested = byte_range.removeprefix("bytes=")
            if requested.startswith("-"):
                range_valid = first == max(0, size - int(requested[1:])) and last == size - 1
            else:
                start, end = map(int, requested.split("-"))
                range_valid = first == start and last == min(end, size - 1)
            range_valid = range_valid and len(payload) == last - first + 1
        return {
            "requested_range": byte_range,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "content_range": content_range,
            "range_valid": range_valid and response.status_code == 206,
            "bytes_read": len(payload),
            "mp4_header": b"ftyp" in payload[:32],
            "seconds": round(time.perf_counter() - started, 3),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--page-url", action="append", help="Detail or episode URL; repeat to check more pages"
    )
    parser.add_argument("--port", type=int, default=18878)
    parser.add_argument(
        "--lookup-proxy", help="Optional explicit HTTP(S) proxy for page and lookup requests"
    )
    parser.add_argument("--preview", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path("data/libvio-exploration/service-e2e.json")
    )
    args = parser.parse_args()
    pages = args.page_url or [DEFAULT_DETAIL_URL]
    base_url = f"http://127.0.0.1:{args.port}"
    application = create_app(
        AppSettings(
            host="127.0.0.1",
            port=args.port,
            database_path=":memory:",
            log_file=None,
            log_level="WARNING",
            upstream_proxy=args.lookup_proxy,
            dav=DavSettings(filename="LIBVIO-test.strm", page_url=pages[0]),
        )
    )

    @application.get("/libvio-check", include_in_schema=False)
    def preview(page_url: str = DEFAULT_DETAIL_URL):
        source = "/play?" + urlencode({"page_url": page_url})
        # Inspect actual decoded frames, rather than counting downloaded bytes.
        return HTMLResponse(
            """<!doctype html><meta charset="utf-8">
<title>LIBVIO playback check</title><style>body{background:#202124;color:white;font:18px sans-serif;padding:24px}video{width:min(100%,900px)}pre{white-space:pre-wrap}</style>
<h1>LIBVIO 自动播放验证</h1><video controls autoplay muted playsinline></video>
<pre id="status">等待视频</pre><script>
const video=document.querySelector('video');const status=document.querySelector('#status');
let frames=0; let event='loading';
function update(){status.textContent=JSON.stringify({event,decodedFrames:frames,currentTime:video.currentTime,duration:video.duration,readyState:video.readyState,error:video.error?.message||null},null,2)}
['loadedmetadata','playing','waiting','error','pause'].forEach(name=>video.addEventListener(name,()=>{event=name;update()}));
function frame(){frames++;event='decoded';update();if(frames<30){video.requestVideoFrameCallback(frame)}else{video.pause();}}
video.requestVideoFrameCallback(frame);
video.src="""
            + json.dumps(source)
            + """;
</script>"""
        )

    server = uvicorn.Server(
        uvicorn.Config(
            application, host="127.0.0.1", port=args.port, log_level="warning", access_log=False
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    report = {"lookup_proxy": bool(args.lookup_proxy), "video_proxy": False, "checks": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with (
            httpx.Client(trust_env=False, timeout=90) as service,
            httpx.Client(
                trust_env=False,
                timeout=30,
                follow_redirects=False,
            ) as media,
        ):
            deadline = time.monotonic() + 15
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("Temporary service did not start")
                time.sleep(0.05)
            service.get(base_url + "/health").raise_for_status()
            dav = service.get(base_url + "/dav/LIBVIO-test.strm", auth=("demo", "demo"))
            dav.raise_for_status()
            report["webdav_status"] = dav.status_code
            for index, page_url in enumerate(pages):
                strm = service.get(base_url + "/strm", params={"page_url": page_url})
                strm.raise_for_status()
                if index == 0 and strm.text != dav.text:
                    raise RuntimeError("WebDAV and /strm returned different playback URLs")
                stable_url = strm.text.strip()
                play = service.get(stable_url, follow_redirects=False)
                if play.status_code != 302 or play.headers.get("cache-control") != "no-store":
                    raise RuntimeError(
                        f"Playback did not return a no-store redirect: {play.status_code} {play.text}"
                    )
                target = play.headers["location"]
                if urlsplit(target).scheme != "https":
                    raise RuntimeError("Video redirect did not use HTTPS")
                head = probe_range(media, target, f"bytes=0-{PROBE_BYTES - 1}")
                if not head["mp4_header"] or not head["range_valid"]:
                    raise RuntimeError("Video did not return a valid MP4 Range response")
                size = int(head["content_range"].rsplit("/", 1)[1])
                middle = size // 2
                ranges = [
                    head,
                    probe_range(
                        media, target, f"bytes={middle}-{min(middle + PROBE_BYTES - 1, size - 1)}"
                    ),
                    probe_range(media, target, f"bytes=-{PROBE_BYTES}"),
                ]
                if not all(r["range_valid"] for r in ranges):
                    raise RuntimeError("Video Range requests failed")
                resolution = service.get(base_url + "/resolve", params={"page_url": page_url})
                resolution.raise_for_status()
                result = resolution.json()
                exported = args.output.parent / f"LIBVIO-test-{index + 1}.strm"
                # This file is for the normal service, rather than the temporary test port.
                exported.write_text(
                    stable_url.replace(base_url, "http://127.0.0.1:8787", 1) + "\n",
                    encoding="utf-8",
                )
                check = {
                    "page_url": page_url,
                    "title": result["title"],
                    "provider": result["provider"],
                    "route": result["route_name"],
                    "episode": result["episode_number"],
                    "lookup_scheme": result["lookup_scheme"],
                    "strm_status": strm.status_code,
                    "play_status": play.status_code,
                    "media_host": urlsplit(target).hostname,
                    "ranges": ranges,
                    "strm_file": str(exported.resolve()),
                }
                report["checks"].append(check)
                print(json.dumps(check, ensure_ascii=False), flush=True)
        report["success"] = True
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Report: {args.output.resolve()}", flush=True)
        if args.preview:
            print(
                "Preview: " + base_url + "/libvio-check?" + urlencode({"page_url": pages[0]}),
                flush=True,
            )
            while thread.is_alive():
                time.sleep(0.5)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
