import pytest
from fastapi.testclient import TestClient

from strm_proxy.app import create_app
from strm_proxy.config import AppSettings
from strm_proxy.dependencies import get_resolver


PAGE_URL = "https://www.xlys02.com/play/27062-0.htm"


class _StubResolver:
    async def fetch_manifest(
        self,
        page_url: str,
        line: int = 0,
        segment_url_builder=None,
    ):
        segment_url = "https://vod.xl01.me/abc.ts"
        if segment_url_builder is not None:
            segment_url = segment_url_builder(segment_url)
        return None, None, f"#EXTM3U\n#EXTINF:6,\n{segment_url}\n"


@pytest.mark.parametrize(
    ("configured", "query_override", "expects_proxy"),
    [
        (True, "false", True),
        (False, "true", False),
    ],
)
def test_hls_uses_global_proxy_setting_and_ignores_query_override(
    configured: bool,
    query_override: str,
    expects_proxy: bool,
) -> None:
    application = create_app(
        AppSettings(database_path=":memory:", proxy_segments=configured)
    )
    application.dependency_overrides[get_resolver] = lambda: _StubResolver()

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        response = client.get(
            "/hls.m3u8",
            params={
                "page_url": PAGE_URL,
                "proxy_segments": query_override,
            },
        )

    assert response.status_code == 200
    assert ("/segment?" in response.text) is expects_proxy


def test_strm_url_is_stable_and_omits_proxy_setting() -> None:
    application = create_app(
        AppSettings(database_path=":memory:", proxy_segments=False)
    )

    with TestClient(application, base_url="http://192.168.1.20:8787") as client:
        response = client.get(
            "/strm",
            params={
                "page_url": PAGE_URL,
                "proxy_segments": "true",
            },
        )

    assert response.status_code == 200
    assert "proxy_segments" not in response.text
    assert "line=0" in response.text
