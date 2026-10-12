# STRM Proxy

[简体中文](README.md) · English

Keep your media player's library up to date while deciding what stays in it.

STRM Proxy turns supported third-party video sources into a WebDAV library that compatible media players can browse and play. Use the web dashboard to manage movies and TV series, keep or hide titles, and refresh episodes. During playback, the service selects a working source and, where applicable, loads upcoming video segments in advance. You can run it on a computer and connect from a TV or phone with NetEase Popcorn (网易爆米花) or another player that supports `.strm` files over WebDAV.

## What you can do

- **Build a library automatically:** Discover new titles and organize movies, TV series, seasons, and episodes into folders your player can browse.
- **Choose what stays:** Search and filter the library, keep favorites, and hide titles you do not want to see in your player.
- **Catch up with TV updates:** Refresh an individual title or check all recently watched series for new episodes in one action.

## A smoother viewing experience

- **Automatic source selection:** When you open a title, the service checks available playback sources and selects one that works.
- **Less repeated waiting:** It remembers a successful source and tries it first next time.
- **Load ahead while watching:** For segmented video handled by the service, upcoming segments are fetched in the background to help reduce waits between segments. Direct video streams are fetched by the player and do not use this feature.
- **Manual control when needed:** Choose a different source from a title's details, then return to automatic selection at any time.

## Get started

You need Python 3.13 or newer, [uv](https://docs.astral.sh/uv/), Node.js, and npm. From the project directory, run:

```powershell
uv sync
npm --prefix frontend ci
npm --prefix frontend run build
uv run strm-proxy
```

The terminal prints the dashboard URL at startup. On the same computer, the default is [http://127.0.0.1:8787/admin/](http://127.0.0.1:8787/admin/). On a new database, both the username and password are `demo`. Change them using the account settings in the top-right corner after signing in. The dashboard and WebDAV share the same credentials.

Your sign-in is kept in the current browser tab. Refreshing the page does not require another login; signing out or closing the tab clears it.

## Connect NetEase Popcorn

1. Make sure the TV or phone running NetEase Popcorn can reach the computer running STRM Proxy.
2. Open the dashboard using that computer's LAN IP address or domain, for example `http://192.168.1.20:8787/admin/`.
3. Click **WebDAV** in the top-right corner to see the read-only connection URL and copy the full connection details.
4. Add a WebDAV source in NetEase Popcorn. Paste the copied details for quick recognition, or enter the address, port, path, username, and password manually. A default WebDAV URL looks like `http://192.168.1.20:8787/dav/`.

The first scan of the movie or TV folder may take a little longer while the library is created. `127.0.0.1` always refers to the device you are using, so a phone or TV needs the address of the computer running STRM Proxy.

## Manage your library

| In the dashboard | What it does |
| --- | --- |
| **Sync a provider (单独同步)** | Discovers only the selected provider's movies and TV series. Each provider shows its title counts, last result, and time. Other providers are not requested or updated. |
| **Sync All (同步全部)** | Updates XLYS and LIBVIO independently. Failed providers keep their existing titles. |
| **Refresh Media (刷新影片)** | Reads that title's details again and updates its episodes if it is a TV series. |
| **Add by URL (通过网址添加)** | Imports an XLYS or LIBVIO detail page and keeps the title in your library. |
| **Refresh Recently Watched Series (刷新最近看过的电视剧)** | Checks series played through this service in the past 30 days and reports new episodes. |
| **Keep / Hide (保留 / 隐藏)** | Kept titles stay through routine syncs; hidden titles do not appear in WebDAV. |
| **Click a poster or title** | Opens details, episodes, source information, library controls, and playback source settings. |

List and grid views support combined provider, media type, and library policy filters, title or year search, sorting, and pagination. TV cards show the number of episodes currently in the library. Choose a provider's **Sync** button or **Sync All** to update; sync is not scheduled automatically. Each provider's last result survives a service restart.

Each provider discovers its 50 most recent titles and highly rated titles from the past four years (movie rating > 7, series rating > 8). LIBVIO titles must offer online playback; download-only listings are skipped. If a provider fails, its previous library is preserved while other providers continue syncing. The dashboard reports incomplete discovery.

LIBVIO currently supports the verified ArtPlayer/SmartPlay MP4 flow, with the player accessing the video CDN directly. Online routes that only offer HLS are not supported yet. Discovery checks for online playback entries; media availability is verified when playback starts.

## If something goes wrong

- **The player cannot connect:** Use the computer's LAN IP in the WebDAV URL rather than the player's own `127.0.0.1`. Check that the devices can reach each other and that the computer's firewall allows the service port.
- **A series is missing episodes:** Open its details in the dashboard and select **Refresh Media**. You can also refresh recently watched series together.
- **A title will not play:** Open its playback source settings and try another source. Playback still depends on the source site's current availability.
- **The player stops connecting after a password change:** Update the saved WebDAV credentials in the player.

WebDAV is for browsing and reading titles; it does not support uploading, renaming, or deleting files. Change the default password before using the service on your LAN. Use HTTPS or a trusted private network across untrusted networks, and do not expose the service directly to the public internet. Only access content you are authorized to use.

For port changes, source sign-in, playback proxy settings, and implementation details, see the [API and WebDAV reference](docs/api-reference.md), [playback resolution guide](docs/play-page-to-strm.md), and [data flow guide](docs/full-data-flow.md) (currently in Chinese).

## Development checks

Install the locked dependencies and run the same checks as CI:

```powershell
uv sync --locked --group dev
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked basedpyright
uv run --locked pytest -q
npm --prefix frontend ci
npm --prefix frontend run lint
npm --prefix frontend run format:check
npm --prefix frontend run check
npm --prefix frontend test
npm --prefix frontend run build
```

Ruff checks and formats Python. Basedpyright uses `strict` for all production code in `strm_proxy`, with ordinary `# type: ignore` disabled and no diagnostic baseline. JSON is typed at the decoding boundary and its structure is validated before fields are used. `typings/` provides types for the PyCryptodome AES interfaces used by the adapters. Tests and live verification scripts are linted and formatted; test doubles are outside the strict type-check scope.

The frontend uses ESLint, Prettier, Svelte/TypeScript checks, and Vitest. Format changes with `uv run ruff format .` and `npm --prefix frontend run format`. Apply safe lint fixes with `uv run ruff check --fix .` and `npm --prefix frontend run lint:fix`.

GitHub Actions runs on pull requests, pushes to `master`, and manual dispatch. Python tests run on Ubuntu with 3.13/3.14 and Windows with 3.13; the frontend uses Node.js 22. Tests use temporary databases and mock upstreams, requiring no credentials, proxy, or live provider. Live verification tools under `scripts/` are run manually and are excluded from CI execution.
