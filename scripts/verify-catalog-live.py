import asyncio
import base64
from urllib.parse import quote

import httpx
from fastapi.testclient import TestClient

from strm_proxy.app import create_app
from strm_proxy.catalog import XlysCatalog
from strm_proxy.config import AppSettings, DEFAULT_USER_AGENT
from strm_proxy.dependencies import get_app_services


async def main() -> None:
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=30,
        headers={"User-Agent": DEFAULT_USER_AGENT},
    ) as client:
        movies = await XlysCatalog(client).discover_movies()
    print(f"movies={len(movies)}", flush=True)
    print(
        f"first={movies[0].dav_filename} -> "
        f"https://www.xlys02.com/play/{movies[0].xlys_id}-0.htm",
        flush=True,
    )
    print(
        f"last={movies[-1].dav_filename} -> "
        f"https://www.xlys02.com/play/{movies[-1].xlys_id}-0.htm",
        flush=True,
    )

    token = base64.b64encode(b"demo:demo").decode()
    headers = {"Authorization": f"Basic {token}"}
    application = create_app(AppSettings(database_path=":memory:"))
    with TestClient(application, base_url="http://192.168.1.20:8787") as test_client:
        get_app_services(application).media_repository.import_discovered_movies(
            movies,
            replace_auto=True,
        )
        root = test_client.request(
            "PROPFIND",
            "/dav/",
            headers={**headers, "Depth": "1"},
        )
        root.raise_for_status()
        assert root.text.count("<D:response>") == 3
        listing = test_client.request(
            "PROPFIND",
            "/dav/%E7%94%B5%E5%BD%B1/",
            headers={**headers, "Depth": "1"},
        )
        listing.raise_for_status()
        assert listing.text.count("<D:response>") == len(movies) + 1
        stream = test_client.get(
            "/dav/%E7%94%B5%E5%BD%B1/" + quote(movies[0].dav_filename, safe=""),
            headers=headers,
        )
        stream.raise_for_status()
        page_url = f"https://www.xlys02.com/play/{movies[0].xlys_id}-0.htm"
        assert quote(page_url, safe="") in stream.text
        assert "proxy_segments" not in stream.text
    print("webdav_root_responses=3 (root + movie + series)", flush=True)
    print(
        f"webdav_movie_responses={len(movies) + 1} "
        "(directory + discovered STRM files)",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
