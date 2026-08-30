# STRM Proxy Demo

一个最小 Python Demo，用来验证以下链路：

```text
xlys 播放页 -> /lines 动态线路 -> 解码包装 M3U8 -> 标准 HLS -> STRM 播放器
```

当前只允许解析本站 HTTPS 播放页，包括新版 `/play/<id>-<episode>.htm` 和旧版 `/<category>/play/<id>-<episode>.htm`，避免把接口变成任意 URL 代理。自动发现会合并两类来源：电影取最近 4 年豆瓣评分严格大于 7.0 的条目，电视剧取最近 4 年严格大于 8.0 的条目；两类媒体都额外合并按更新时间排序的最近 50 条。结果持久化到 SQLite，后续 WebDAV 扫描只读取数据库。请仅用于你有权访问和播放的内容。

## 架构

```text
网易爆米花
  -> WebDAV /dav/（根目录发现电影/、电视剧/）
  -> WebDAV /dav/电影/（读取 SQLite 中可见的 STRM）
  -> SQLAlchemy / SQLite（电影、电视剧、分集、豆瓣评分、保留策略）
  -> API /hls.m3u8（稳定播放入口）
  -> XlysResolver（页面、签名线路、缓存）
  -> HLS codec（清单 gzip 解包、TS 的 PNG 解包）
  -> 上游分片 CDN
```

代码按职责拆分：

| 模块 | 职责 |
| --- | --- |
| `app.py` | 应用装配、生命周期、共享 HTTP 客户端和异常映射 |
| `config.py` | 环境变量和强类型配置 |
| `routes.py` | `/resolve`、`/hls.m3u8`、`/strm`、`/segment` API |
| `catalog.py` | 电影/电视剧评分与最近更新抓取、卡片解析、去重和缓存 |
| `database.py` | SQLAlchemy 统一媒体/分集模型、SQLite 迁移和持久化查询 |
| `library.py` | 首次导入、电视剧详情补抓、可见策略和播放 URL 生成 |
| `webdav.py` | 只读 WebDAV、电影/电视剧/分集目录、Basic Auth 和 DAV XML |
| `admin.py` | 管理 API、影片策略修改、网址导入和手动同步 |
| `detail.py` | 详情页 URL 校验、电影/电视剧识别和分集解析 |
| `xlys.py` | 站点 URL 校验、AES 签名、线路解析和缓存编排 |
| `hls.py` | M3U8 解压/重写和 MPEG-TS 解包 |
| `cache.py` | 独立 TTL 内存缓存 |
| `models.py` | 领域模型与解析异常 |

`resolver.py` 只保留旧导入路径兼容层，新代码不再把不同职责堆在其中。

播放页如何经过动态签名、线路选择、M3U8/TS 解包，最终成为 STRM 中的本地播放 URL，详见[《从播放页面到 STRM：解析链路说明》](docs/play-page-to-strm.md)。

从资源发现、SQLite 入库、WebDAV 扫描到实际分片播放的全局视图，详见[《STRM Proxy 完整数据链路总览》](docs/full-data-flow.md)。

所有播放、管理和 WebDAV 接口的参数、响应与状态码，详见[《STRM Proxy API 接口文档》](docs/api-reference.md)。

## 启动

```powershell
uv sync
uv run strm-proxy
```

默认监听 `0.0.0.0:8787`。API 文档位于：

```text
http://127.0.0.1:8787/docs
```

管理界面位于 `http://127.0.0.1:8787/admin/`，登录账号与 WebDAV 相同。首次运行或前端代码变化后先构建静态文件：

```powershell
cd frontend
npm install
npm run build
cd ..
uv run strm-proxy
```

开发管理界面时，在后端运行的同时执行 `npm run dev`；Vite 会把 `/api` 请求代理到本机 `8787` 端口。

## 通过 WebDAV 接入网易爆米花

Demo 内置只读 WebDAV 媒体库。爆米花只需填写 `/dav/`：根目录返回 `电影/` 和 `电视剧/` 两个分类；电视剧按“剧名 / Season XX / SxxExx.strm”组织。原有的 `/dav/痴迷.strm` 仍可直接访问用于单片播放回归，但不会出现在根目录扫描结果中。

| 配置项 | 填写值 |
| --- | --- |
| 协议 | HTTP |
| 主机 | 运行本服务的电脑/NAS 局域网 IP |
| 端口 | `8787` |
| 路径 | `/dav/` |
| 用户名 | `demo` |
| 密码 | `demo` |

例如服务端 IP 是 `192.168.1.20`，WebDAV 地址就是：

```text
http://192.168.1.20:8787/dav/
```

不要在手机或电视上的爆米花中填写 `127.0.0.1`。如果无法连接，先确认两台设备处于同一局域网，并检查 Windows 防火墙是否允许 TCP 8787 入站。

默认值可以用环境变量覆盖：

```powershell
$env:STRM_PROXY_DAV_USER = 'myuser'
$env:STRM_PROXY_DAV_PASSWORD = 'change-me'
$env:STRM_PROXY_DAV_FILENAME = '痴迷.strm'
$env:STRM_PROXY_PAGE_URL = 'https://www.xlys02.com/play/27062-0.htm'
$env:STRM_PROXY_PROXY_SEGMENTS = 'true'
$env:STRM_PROXY_CACHE_TTL = '300'
$env:STRM_PROXY_DISCOVERY_RECENT_LIMIT = '50'
$env:STRM_PROXY_DISCOVERY_YEAR_SPAN = '4'
$env:STRM_PROXY_DOUBAN_RATING_THRESHOLD = '7.0'
$env:STRM_PROXY_SERIES_DOUBAN_RATING_THRESHOLD = '8.0'
$env:STRM_PROXY_CATALOG_CACHE_TTL = '1800'
$env:STRM_PROXY_DATABASE_PATH = 'data/strm-proxy.db'
$env:STRM_PROXY_DAV_CATALOG_DIRECTORY = '电影'
$env:STRM_PROXY_DAV_SERIES_DIRECTORY = '电视剧'
uv run strm-proxy
```

该 WebDAV 仅实现爆米花扫描 STRM 所需的只读方法：`OPTIONS`、`PROPFIND`、`GET` 和 `HEAD`，不支持上传、删除或改名。默认账号只适合局域网验证，不要直接暴露到公网。

默认数据库位于 `data/strm-proxy.db`。数据库为空时，第一次扫描电影目录会抓取列表并写入；数据库已有影片后，服务重启和 WebDAV 扫描都不会再次访问列表页。本阶段尚未加入定时刷新。

`media_items` 统一保存电影和电视剧元数据及豆瓣评分，`episodes` 保存电视剧分集及源站真实播放路径；旧版 `movies` 表会在启动时迁移后移除。`policy` 支持 `auto`、`keep`、`hidden`；每次同步会替换已经不在当前发现集合中的 `auto` 记录，同时保留人工 `keep` 和 `hidden`。解析后的页面线路和标准 M3U8 仍在内存中缓存 5 分钟。

服务启动后，可以运行仓库内的 E2E 脚本验证 `WebDAV -> STRM -> M3U8 -> TS` 整条链路：

```powershell
.\scripts\verify-webdav-e2e.ps1
```

旧的单类型目录发现回归脚本：

```powershell
.\.venv\Scripts\python.exe .\scripts\verify-catalog-live.py
```

验证真实的电影/电视剧评分与最近更新并集：

```powershell
uv run python scripts\verify-discovery-live.py
```

测试环境中清空旧媒体并导入一份完整的真实发现快照：

```powershell
uv run python scripts\import-discovery-live.py
```

## 验证目标页面

先查看解析出的线路：

```powershell
$page = [uri]::EscapeDataString('https://www.xlys02.com/play/27062-0.htm')
Invoke-RestMethod "http://127.0.0.1:8787/resolve?page_url=$page"
```

将解码后的标准 M3U8 保存下来：

```powershell
Invoke-WebRequest "http://127.0.0.1:8787/hls.m3u8?page_url=$page" -OutFile decoded.m3u8
Get-Content decoded.m3u8 -TotalCount 10
```

用 VLC 或 mpv 进行播放验证：

```powershell
mpv "http://127.0.0.1:8787/hls.m3u8?page_url=$page"
```

这个站点的 TS 分片也包装成了 1×1 PNG，因此默认启用分片代理。代理会去掉 PNG 外壳并返回标准 `video/mp2t`。该模式是服务级全局配置，不会写入 STRM URL；如需让 TV 直连上游分片，请在启动服务前设置：

```powershell
$env:STRM_PROXY_PROXY_SEGMENTS = 'false'
uv run strm-proxy
```

修改此环境变量后需要重启服务。旧 STRM URL 即使仍带有 `proxy_segments` 查询参数，服务也会忽略它并使用全局配置。

## 生成 STRM 内容

```powershell
Invoke-WebRequest "http://127.0.0.1:8787/strm?page_url=$page" | Select-Object -Expand Content
```

把返回的单行 URL 保存为例如 `痴迷 (2021).strm`，再通过本地目录或 WebDAV 导入网易爆米花。

注意：爆米花运行在电视或手机上时，STRM 内不能使用 `127.0.0.1`，应改成运行本服务的电脑或 NAS 的局域网 IP，例如 `http://192.168.1.20:8787/...`。

## 接口

- `GET /health`：健康检查。
- `GET /resolve?page_url=...`：查看 PID 和可用 M3U8 线路。
- `GET /hls.m3u8?page_url=...&line=0`：返回标准 HLS manifest；是否代理 TS 由全局环境变量决定。
- `GET /strm?page_url=...`：返回适合写入 `.strm` 的单行 URL。
- `GET /segment?...`：TS 分片解包代理，只允许 `vod.xl01.me`。
- `GET /api/admin/movies`：返回管理界面的全部影片和策略统计，需要 Basic Auth。
- `PATCH /api/admin/movies/{xlys_id}/policy`：设置 `auto`、`keep` 或 `hidden`。
- `PATCH /api/admin/movies/batch/policy`：批量设置影片策略。
- `DELETE /api/admin/movies/batch`：批量删除数据库影片记录。
- `POST /api/admin/import`：通过详情页网址手动添加；电影和电视剧都会写入数据库并设为人工保留。
- `POST /api/admin/sync`：立即从来源同步最新影片。

## 测试

```powershell
uv run pytest
```
