# STRM Proxy API 接口文档

本文记录当前服务实际提供的 HTTP API、管理 API 和只读 WebDAV 接口。默认基础地址为：

```text
http://127.0.0.1:8787
```

部署到其他设备后，将示例中的基础地址替换为服务器地址。交互式 OpenAPI 页面位于 `/docs`，原始 OpenAPI JSON 位于 `/openapi.json`；WebDAV 接口不会出现在 OpenAPI 中。

## 1. 接口与鉴权总览

| 接口组 | 路径 | 鉴权 | 主要用途 |
| --- | --- | --- | --- |
| 播放 API | `/health`、`/resolve`、`/play`、`/hls.m3u8`、`/strm`、`/segment` | 无 | 健康检查、线路解析和播放 |
| 管理 API | `/api/admin/*` | HTTP Basic | 查询、同步、导入和修改媒体策略 |
| WebDAV | `/dav/` | HTTP Basic，`OPTIONS` 除外 | 向 STRM 播放器暴露虚拟媒体库 |
| 管理界面 | `/admin/` | 页面静态资源无鉴权；API 有 Basic Auth | 浏览器管理媒体库 |

接口行为相关的主要环境变量：

| 环境变量 | 默认值 |
| --- | --- |
| `STRM_PROXY_HOST` | `0.0.0.0` |
| `STRM_PROXY_DAV_USER` | `demo` |
| `STRM_PROXY_DAV_PASSWORD` | `demo` |
| `STRM_PROXY_PROXY_SEGMENTS` | `true` |
| `STRM_PROXY_UPSTREAM_PROXY` | 未设置；例如 `http://127.0.0.1:7890` |
| `STRM_PROXY_PLAY_SELECTION_CACHE` | `true` |
| `STRM_PROXY_PLAY_SELECTION_TTL_SECONDS` | `7200` 秒（2 小时，仅自动选择） |
| `STRM_PROXY_SEGMENT_PREFETCH_SECONDS` | `60` 秒 |
| `STRM_PROXY_SEGMENT_CACHE_MAX_MB` | `128` MiB；`0` 表示关闭 |
| `STRM_PROXY_LOG_LEVEL` | `INFO` |
| `STRM_PROXY_LOG_FILE` | `data/strm-proxy.log`；空字符串表示关闭文件日志 |
| `STRM_PROXY_XLYS_USERNAME` | 未设置 |
| `STRM_PROXY_XLYS_PASSWORD` | 未设置 |
| `STRM_PROXY_REQUEST_TIMEOUT` | `20` 秒 |
| `STRM_PROXY_CONNECT_TIMEOUT` | `10` 秒 |

两个 xlys 登录变量必须同时设置；其值对应站点实际使用的 `username`、`password` Cookie 值，而不是 WebDAV 账号。Cookie 按 xlys 域限定，不会发送给媒体 CDN。

未设置 `STRM_PROXY_UPSTREAM_PROXY` 时，上游 HTTP 请求使用 HTTPX 默认的系统或环境代理配置。设置该变量后，XLYS 的服务端上游 HTTP 请求统一经过指定的 HTTP(S) 代理，包括来源页面和接口、HLS manifest、前台分片及后台预读。启动日志中的 `upstream_proxy=true|false` 仅表示是否指定了此覆盖值，不会记录代理地址或其中的认证信息。

LIBVIO 的试验接入使用独立客户端：页面和解析请求默认直连，设置 `STRM_PROXY_UPSTREAM_PROXY` 后使用指定代理；视频探测始终直连，播放器通过 302 直接访问 HTTPS 视频 CDN。LIBVIO 客户端不继承系统或环境代理。

默认监听 `0.0.0.0`，允许局域网设备访问。WebDAV 账号密码在首次建库时由环境变量初始化，此后保存在数据库中，可在管理页修改；环境变量的后续变化不会覆盖数据库凭据。建议修改默认账号密码。HTTP Basic 凭据应通过 HTTPS 或可信私有网络传输。当前播放 API 没有鉴权，不应直接无保护地暴露到公网。

## 2. 播放 API

### 2.1 `GET /health`

健康检查，不访问上游站点。

响应：

```json
{
  "status": "ok"
}
```

### 2.2 `GET /resolve`

解析播放页并返回当前可用线路，不获取或返回 TS 分片。

查询参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `page_url` | string | 是 | — | xlys 播放页或 LIBVIO 详情/播放页 HTTPS URL，应进行 URL 编码 |

xlys 的 `page_url` 只允许配置中的 xlys 域名，并且路径必须符合以下形式之一：

```text
/play/<id>-<episode>.htm
/<category>/play/<id>-<episode>.htm
```

示例：

```text
GET /resolve?page_url=https%3A%2F%2Fwww.xlys02.com%2Fplay%2F27062-0.htm
```

响应示例：

```json
{
  "page_url": "https://www.xlys02.com/play/27062-0.htm",
  "pid": 204486,
  "title": "痴迷",
  "sources": {
    "hls": true,
    "tos": true,
    "member": true
  },
  "lines": [
    {
      "index": 0,
      "name": "iplay",
      "kind": "m3u8",
      "url": "https://example.invalid/path/video.m3u8#iplay"
    }
  ]
}
```

`sources` 只表示 `/lines` 是否公布对应能力，不表示媒体 CDN 已经验证可用；`/play` 会在返回直连地址前做小型 Range 探测。HLS 线路数量就是 `lines` 数组长度：服务会拆分上游 HLS 字段中的逗号分隔 URL，过滤非 M3U8 项并按 URL 去重。`direct_lines` 单独报告 `url3` 直连线路的序号，不返回临时签名 URL。`lines[].index` 只是当前解析结果中的诊断信息，不是播放接口参数。`lines[].name` 来自 URL fragment（例如 `#iplay`）；fragment 不会发送给媒体 CDN，是持久化选择使用的稳定标识。没有 fragment 时该字段为 `null`。

#### LIBVIO 试验接入

`/resolve`、`/play`、`/strm` 支持 `https://www.libvio.lat/detail/<id>.html` 和 `https://www.libvio.lat/play/<id>-<sid>-<nid>.html`，也接受不带 `www` 的域名。详情页选择可用在线线路的第一集；播放页保留指定线路和集数。网盘下载线路会被跳过，路径中的公开媒体 ID 不会被播放器内部 ID 替换。

LIBVIO 的 `/resolve` 返回 `provider: "libvio"`、`source: "direct"`、`title`、`play_page_url`、`public_media_id`、`route_name`、`episode_number`、`lookup_scheme` 和临时 `media_url`。它会先检查 MP4 数据是否可读。`/play` 仅支持 `source=auto`，返回不缓存的 302；`/strm` 保存稳定的 `/play?page_url=...`，每次播放都能重新取得临时签名地址。

当前实现覆盖已验证的 ArtPlayer MP4 流程。解析结果最多缓存 60 秒，命中后会重新探测；失效时重新解析。SmartPlay 的固定 HTTPS 查询接口遇到 `WRONG_VERSION_NUMBER` 时，使用同一接口提供的 HTTP 地址兼容当前测试网络；不会因证书验证错误降级，页面和视频仍使用 HTTPS。解析参数是公开播放页的参数，不携带 XLYS 登录 Cookie。

可通过以下入口试验：

```text
GET /strm?page_url=https%3A%2F%2Fwww.libvio.lat%2Fdetail%2F5813847.html
```

管理页“通过网址添加”及 `POST /api/admin/import` 支持 LIBVIO 详情页。导入后设为人工保留，电影显示在 WebDAV 的“电影”目录，电视剧按季度和分集显示；支持刷新信息、保留、隐藏和删除。“供应商同步”也会自动发现 LIBVIO 内容；人工保留的条目不会被轮换掉。LIBVIO 当前自动选择直连线路，管理页不提供人工固定线路。

自动发现只读取分类及详情，跳过下载专用条目；视频是否可读在播放时检查。部分在线线路返回 HLS，当前 LIBVIO 播放适配仍仅支持前述 MP4 流程；例如抽查的《挽救计划》HD7 返回 HLS，暂不能通过本适配器播放。

设置 `STRM_PROXY_PAGE_URL` 为该详情页、`STRM_PROXY_DAV_FILENAME` 为 `LIBVIO.strm` 后，兼容的单文件 WebDAV 入口 `/dav/LIBVIO.strm` 也可用于播放。

独立验证工具使用内存数据库和临时本地服务，检查 WebDAV、STRM、302 跳转及视频头部、中部和尾部的 Range 响应；报告不包含临时签名 URL。可用 `--page-url` 重复指定详情页或分集页，`--lookup-proxy` 仅设置页面和解析代理，`--preview` 保持浏览器验证入口运行：

```powershell
uv run python scripts/verify_libvio_e2e.py --page-url https://www.libvio.lat/detail/5813847.html
```

### 2.3 `GET` 或 `HEAD /play`

稳定的协议无关播放入口。默认 `source=auto`：先查询持久化播放决策；命中人工固定的 `url3`、TOS 或命名 HLS 时优先使用该来源。未命中或缓存方向失效时，TOS、member 和全部 HLS 候选并发探测，最先完成最多 512 KiB 媒体样本且格式有效的来源获胜；`url3` 仅在管理页人工选择时使用。HLS 会解包 manifest 并读取首个媒体资源，TOS 使用普通流式 GET 读取等大的样本，因此 TOS 与 HLS 参加同一次速度竞争。member 当前会因自动验证码识别尚未配置而退出竞争。

同一 `page_url` 在一个服务进程内只会同时运行一个探索任务。并发请求共享结果；等待中的客户端超时或断开只会取消该客户端的等待，不会取消探索任务，成功结果仍会写入 SQLite 供客户端后续重试。

`/lines.data.ptoken` 只表示页面公布了受验证码保护的会员线路，不是可直接提交的验证码。站点验证码是一道整数加、减、乘算术题，要求在同一会话中提交计算结果。当前版本尚未启用自动 OCR，也不提供人工预验证入口；`source=member` 会报告识别器未配置，`source=auto` 中其他并发的 TOS/HLS 探测会继续进行。

查询参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `page_url` | string | 是 | — | xlys 播放页 URL |
| `source` | enum | 否 | `auto` | `auto`、`hls`、`tos` 或 `member` |

成功响应为不缓存的 `302`：

```http
HTTP/1.1 302 Found
Cache-Control: no-store
Location: https://.../video-object
```

TOS 成功时，TV 直接访问媒体 CDN，视频数据不经过本服务。自动探索只为每个候选读取最多 512 KiB 媒体样本，不下载完整视频；缓存命中的 TOS 仍只用 `Range: bytes=0-63` 做轻量可用性复核。登录 Cookie 只用于 xlys 播放页和 `/lines`，不会发送给媒体 CDN。`ptoken` 不会被当作验证码提交。

播放决策保存在 SQLite 的通用 `cache_entries` 表中，服务重启后仍然有效。自动选择默认在 7200 秒后过期，下一次播放会重新测速；可通过 `STRM_PROXY_PLAY_SELECTION_TTL_SECONDS` 设置正数秒值。管理页人工固定的线路没有该有效期，直到用户恢复自动或来源失效。带名称的 HLS 缓存项以 `route_name` 为稳定身份；每次解析页面后再把它映射为当次数字 ID，因此上游调整 line 顺序、字段或 URL 后仍可复用。HLS 决策还包含持久化的 `revision`，并作为 `/hls.m3u8` 的 `v` 参数下发。重新选择线路或缓存失效后重新探索会生成新版本；仅把相同线路名映射到新的数字 ID 不会改版本。数字 line 只作为一次请求内部的临时索引。旧的无名称缓存继续按 line、候选类型和 URL 严格校验；旧版缓存缺少 revision 或过期时间时会自动补齐并写回。线路消失、名称歧义或 manifest 获取失败时缓存自动删除。设置 `STRM_PROXY_PLAY_SELECTION_CACHE=false` 可以关闭持久化的成功/失败缓存，使每个请求都重新探索；视频级并发合并仍然生效，同时管理页也不能保存人工线路。

如果页面公布的所有来源都失败，响应为 `503 Service Unavailable`，并包含 `Retry-After: 30` 与 `X-STRM-Proxy-Error: no-playable-source`。JSON 的 `detail.errors` 会列出 TOS、member 和 HLS 的失败原因，便于非播放器客户端诊断。相同页面的失败结果缓存 30 秒，期间重试直接返回相同的 `503`；本服务不生成错误视频。

### 2.4 `GET /hls.m3u8`

带版本的 HLS 播放入口。服务端查询播放决策缓存，以线路名称重新映射当前 ID，然后还原包装后的 M3U8，并根据全局环境变量决定是否代理 TS 分片。正常客户端从 `/play` 重定向取得当前版本，无须自行管理 `v`。

查询参数：

| 参数 | 类型 | 必填 | 默认值 | 约束与说明 |
| --- | --- | --- | --- | --- |
| `page_url` | string | 是 | — | xlys 播放页 HTTPS URL，应进行 URL 编码 |
| `v` | string | 否 | — | 服务端生成的不透明 HLS 决策版本；缺失或不是当前版本时重定向到当前 URL |

旧客户端即使仍附带 `line=N`，该参数也会被忽略，实际线路完全由服务端缓存和探索结果决定。

当 `v` 缺失或已经过期时，接口不会在旧 URL 下直接返回新清单，而是返回不缓存的 `302`：

```http
HTTP/1.1 302 Found
Cache-Control: no-store
Location: /hls.m3u8?page_url=...&v=<current>
```

播放器随后请求新 URL。HLS 时间轴本身不因版本参数改变，正常的观看进度仍由播放器/媒体库保存；版本变化只会让 manifest 和分片 URL 使用新的缓存键。

两种分片模式：

| `STRM_PROXY_PROXY_SEGMENTS` | 返回清单中的 TS 地址 | 数据链路 | 兼容性与流量 |
| --- | --- | --- | --- |
| `true` | `http(s)://本服务/segment?...` | TV → 本服务 → 上游 CDN | 兼容性高，全部视频流量经过本服务 |
| `false` | `https://vod.xl01.me/...ts` | TV → 上游 CDN | 本服务流量很小；自动选线优先原生分片，全部线路均有包装时仍取决于播放器兼容性 |

未命中缓存时，各 HLS 线路会并发读取首个媒体资源的固定 512 KiB 样本。`STRM_PROXY_PROXY_SEGMENTS=true` 时，包装线路和原生线路都能由服务端处理，样本最先完成且健康的线路获胜；设为 `false` 时，服务优先选择 MPEG-TS 从第 0 字节开始或标准 fMP4 的原生线路，全部健康线路均为图片包装时才回退到包装线路。HLS 的优胜者还会与并发的 TOS 样本比较。需要人工固定线路时使用管理页按 `route_name` 保存，而不是把数字 line 写入播放 URL。

这些配置在服务启动时读取，修改后必须重启。旧客户端 URL 中即使仍有 `proxy_segments=true|false`，也会被当作未知查询参数忽略。`STRM_PROXY_PROXY_SEGMENTS=false` 只让 TS 分片直连；M3U8 仍由本服务获取、解包并返回，因为上游原始 M3U8 不是标准明文清单。

响应：

```http
Content-Type: application/vnd.apple.mpegurl
Cache-Control: no-store
```

正文是标准 `#EXTM3U` 文本，例如：

```m3u8
#EXTM3U
#EXT-X-VERSION:3
#EXTINF:12.0,
http://127.0.0.1:8787/segment?url=...
```

### 2.5 `GET /strm`

生成适合写入 `.strm` 文件的一行播放 URL。本接口不解析上游播放页，只负责构造稳定的 `/play` 地址。

查询参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `page_url` | string | 是 | — | xlys 播放页 URL |
| `source` | enum | 否 | `auto` | `auto`、`hls`、`tos` 或 `member`；`auto` 不写入返回 URL |

响应：

```http
Content-Type: text/plain; charset=utf-8
```

```text
http://127.0.0.1:8787/play?page_url=...
```

返回 URL 的协议和主机来自当前 HTTP 请求。经过反向代理时，必须正确传递外部 Host 和协议，否则 STRM 中可能出现内网地址或错误的 `http` 协议。

### 2.6 `GET /segment`

获取一个上游 TS 分片，寻找 MPEG-TS 同步位置，只流式返回完整且同步的 188 字节 MPEG-TS 包。

查询参数：

| 参数 | 类型 | 必填 | 默认值 | 约束与说明 |
| --- | --- | --- | --- | --- |
| `url` | string | 是 | — | 上游 TS URL；必须是 HTTPS，主机必须等于配置的分片主机 |
| `referer` | string | 否 | `null` | 请求上游分片时使用的 `Referer`，通常是 xlys 播放页 URL |
| `playlist` | string | 否 | `null` | `/hls.m3u8` 生成的内部播放列表标识，用于定位后续分片 |
| `index` | integer | 否 | `null` | 当前分片在播放列表中的索引，用于启动前向预读 |
| `v` | string | 否 | `null` | 生成该分片 URL 的 HLS revision；由 manifest 自动下发 |

服务会记住每个播放页最近生成的 playlist。若请求中的 playlist、URL 或 `v` 已经过期，但 index 仍能在当前 playlist 中定位，接口返回 `Cache-Control: no-store` 的 `302`，指向当前线路相同 index 的分片。这样播放器即使保留了旧 manifest 中已经排队的分片 URL，也不会继续访问旧线路。

默认只允许：

```text
https://vod.xl01.me/...
```

响应：

```http
Content-Type: video/mp2t
```

正文从第一个可信 MPEG-TS 同步包开始。服务最多在上游响应的前 64 KiB 中寻找三个间隔 188 字节的 `0x47` 同步字节；未找到时返回 `502`。传输途中会在出现包装尾部或填充数据时重新同步，并丢弃末尾不足 188 字节的残片。

前台缓存未命中时，该接口仍然在找到 TS 起点后立即流式返回，不会为了缓存而等待整段下载完成；完整分片传输结束后，已经去除包装的数据写入全局内存 LRU 缓存。相同分片正在下载时，后续请求会等待并复用同一个结果。

当 URL 包含有效的 `playlist` 和 `index` 时，服务根据 M3U8 中的 `EXTINF` 从下一段开始向后预读，累计约 `STRM_PROXY_SEGMENT_PREFETCH_SECONDS` 秒后停止。后台下载全局最多并发 2 路，并受 `STRM_PROXY_SEGMENT_CACHE_MAX_MB` 容量限制。预读失败不会写入错误或残缺内容。上述行为仅在 `STRM_PROXY_PROXY_SEGMENTS=true` 且容量大于零时启用。

### 2.7 `HEAD /segment`

参数与 `GET /segment` 相同。已有内存缓存时直接返回缓存长度；未命中时向上游发送 `HEAD`。HEAD 本身不启动预读，也不下载或验证 TS 内容。

## 3. 管理 API

所有管理接口使用 HTTP Basic Auth：

```bash
curl -u demo:demo http://127.0.0.1:8787/api/admin/media
```

下文使用 `/api/admin/media` 作为规范路径。当前仍保留以下兼容别名，但它们不会出现在 OpenAPI 页面中：

| 兼容路径 | 等价规范路径 |
| --- | --- |
| `/api/admin/movies` | `/api/admin/media` |
| `/api/admin/movies/{xlys_id}/policy` | `/api/admin/media/{xlys_id}/policy` |
| `/api/admin/movies/batch/policy` | `/api/admin/media/batch/policy` |
| `/api/admin/movies/batch` | `/api/admin/media/batch` |

### 3.1 媒体对象 `MovieItem`

由于历史兼容原因，响应模型仍命名为 `MovieItem`，但其中可以同时包含电影和电视剧。

| 字段 | 类型 | 可空 | 说明 |
| --- | --- | --- | --- |
| `xlys_id` | integer | 否 | 兼容字段：管理 API 使用的内部媒体 ID；已有 XLYS ID 保持原值，其他供应商使用独立的负数 ID |
| `provider` | enum | 否 | `xlys` 或 `libvio` |
| `source_media_id` | string | 否 | 供应商实际媒体 ID；与内部 ID 分开保存 |
| `title` | string | 否 | 标题 |
| `year` | integer | 是 | 年份 |
| `cover_url` | string | 是 | 封面地址 |
| `douban_rating` | number | 是 | 豆瓣评分 |
| `source_updated_on` | `YYYY-MM-DD` | 是 | 来源更新时间 |
| `policy` | enum | 否 | `auto`、`keep` 或 `hidden` |
| `dav_filename` | string | 否 | WebDAV 文件名或电视剧目录名 |
| `play_page_url` | string | 否 | 电影首集或媒体播放页 URL |
| `kind` | enum | 否 | `movie` 或 `series` |
| `available_episode_count` | integer | 否 | 已收录分集数；电影为 `0` |

成功的 `GET /play` 请求会记录已入库电视剧的最近播放时间；`HEAD /play` 和 WebDAV 扫描不会记录。该记录从启用此版本后开始积累。

策略含义：

| 值 | 含义 |
| --- | --- |
| `auto` | 由自动发现管理；下次完整同步不再命中时可被移除 |
| `keep` | 人工保留，不因自动发现集合变化而移除 |
| `hidden` | 保留数据库记录，但不在 WebDAV 可见目录中显示 |

### 3.2 媒体目录响应 `MovieCatalog`

```json
{
  "movies": [],
  "counts": {
    "total": 0,
    "automatic": 0,
    "kept": 0,
    "hidden": 0,
    "movies": 0,
    "series": 0
  },
  "recent_limit": 50,
  "recently_watched_series_count": 0,
  "recently_watched_series": [],
  "discovery_failures": [],
  "provider_syncs": [
    {"provider": "libvio", "status": "idle", "last_synced_at": null, "failures": []},
    {"provider": "xlys", "status": "idle", "last_synced_at": null, "failures": []}
  ]
}
```

`movies` 字段同样是历史名称，数组中可能包含 `kind=series` 的电视剧。
`recently_watched_series_count` 是近 30 天通过 `/play` 播放过的已入库电视剧数量。

`provider_syncs` 分别显示已启用供应商的同步状态：`idle`、`syncing`、`success`、`partial` 或 `failed`。`last_synced_at` 是最近一次完成尝试的时间（成功或失败），`failures` 是该供应商的失败说明。已完成的结果存入 SQLite，重启后保留；正在同步的临时状态不持久化。

### 3.3 `GET /api/admin/media`

返回数据库中全部受管理媒体，包括 `hidden` 条目。

响应类型：`MovieCatalog`。

### 3.3.1 `GET /api/admin/media/{xlys_id}`

读取单部影片的数据库详情，不访问上游。响应包含 `MovieItem` 的全部字段，另有 `source_url`、`declared_episode_count`、`season_number`、`last_checked_at`、`last_watched_at` 和 `episodes`。电视剧的 `episodes` 按来源序号排序，每项含 `source_index`、`label` 和 `play_page_url`；电影返回空数组。影片不存在返回 `404`。

### 3.4 `PATCH /api/admin/media/{xlys_id}/policy`

修改单个媒体的策略。

路径参数：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `xlys_id` | integer | 目标媒体 ID |

请求体：

```json
{
  "policy": "keep"
}
```

响应类型：更新后的 `MovieItem`。ID 不存在时返回 `404`。

### 3.5 `PATCH /api/admin/media/batch/policy`

批量修改策略。

请求体：

```json
{
  "xlys_ids": [27062, 27063],
  "policy": "hidden"
}
```

| 字段 | 约束 |
| --- | --- |
| `xlys_ids` | 1 至 500 个整数；服务会对重复 ID 去重 |
| `policy` | `auto`、`keep` 或 `hidden` |

响应类型：修改后的完整 `MovieCatalog`。

### 3.6 `DELETE /api/admin/media/batch`

批量删除数据库媒体记录。电视剧分集通过外键级联删除。

请求体：

```json
{
  "xlys_ids": [27062, 27063]
}
```

`xlys_ids` 必须包含 1 至 500 个整数。响应类型为删除后的完整 `MovieCatalog`。

### 3.7 `POST /api/admin/sync`

立即执行电影和电视剧完整发现，将结果写入 SQLite。

请求体：无。

可选查询参数 `provider=libvio` 或 `provider=xlys` 表示只同步该供应商，其他供应商不会被请求、更新或清理。省略时同步所有启用的供应商，兼容原有调用。供应商之间使用独立锁，网站关闭或请求等待不会阻塞其他供应商的单独同步。

同步行为：

1. 分别从 XLYS 和 LIBVIO 抓取电影、电视剧的最近更新集合（默认各 50 条）；
2. 抓取近 4 年的评分集合，电影评分严格大于 7，电视剧严格大于 8；
3. 按供应商和来源 ID 合并、去重并补抓详情；LIBVIO 跳过仅有网盘下载的影片；
4. 按供应商及媒体类型替换不再出现在完整发现集合中的 `auto` 记录；
5. 保留 `keep`、`hidden` 和近 30 天看过的电视剧；
6. 某个供应商分类抓取失败时保留其原有片库，其他分类继续同步。LIBVIO 部分详情失败时更新成功条目，但不移除该类型的旧条目；空结果也不会清空片库。

该请求需要访问上游，可能持续较长时间。响应类型为同步后的 `MovieCatalog`，`discovery_failures: string[]` 仅列出本次所选供应商未完成的部分；为空表示本次发现完整完成。所选供应商分类全部失败时返回 `502`；其结果仍可通过 `GET /api/admin/media` 的 `provider_syncs` 查看，既有片库保留。

### 3.8 `POST /api/admin/import`

从一个 XLYS 或 LIBVIO 详情页手动导入电影或电视剧，并将策略设置为 `keep`。LIBVIO 的输入格式为 `https://www.libvio.lat/detail/<id>.html`；导入时跳过网盘下载线路。重复添加同一供应商、同一来源 ID 会更新既有条目，不会重复建库，也不会与其他供应商的相同 ID 相互覆盖。

请求体：

```json
{
  "url": "https://www.xlys02.com/guoju/12345.htm"
}
```

| 字段 | 约束 |
| --- | --- |
| `url` | 1 至 2048 个字符；必须是允许域名上的 HTTPS 详情页；不能包含凭据、自定义端口、查询参数或 fragment |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `kind` | `movie` 或 `series` | 识别出的媒体类型 |
| `imported` | boolean | 是否完成导入 |
| `xlys_id` | integer | 媒体 ID |
| `provider` | enum | `xlys` 或 `libvio` |
| `source_media_id` | string/null | 来源 ID；LIBVIO 始终返回该字段 |
| `title` | string | 标题 |
| `year` | integer/null | 年份 |
| `cover_url` | string/null | 封面 URL |
| `available_episode_count` | integer | 当前发现的分集数量 |
| `declared_episode_count` | integer/null | 详情页声明的总集数 |
| `source_url` | string | 规范化后的详情页 URL |
| `message` | string | 导入结果说明 |
| `catalog` | `MovieCatalog`/null | 更新后的媒体目录 |

### 3.9 手动刷新影片与最近观看的电视剧

`POST /api/admin/media/{xlys_id}/refresh` 从该影片已保存的来源详情页重新读取信息。电视剧分集按当前详情页增删和更新；片库策略与 WebDAV 名称保持原值。影片不存在返回 `404`，没有详情页地址或详情页与原影片不匹配返回 `409`。响应包含 `item`（影片 ID、标题、类型、新增集数、当前集数）和更新后的 `catalog`。

`POST /api/admin/media/refresh-recent-series` 一次刷新近 30 天通过 `GET /play` 成功播放过的全部已入库电视剧。它逐部处理，单部失败不会阻断其他影片。响应包含 `checked`、`refreshed`、`added_episodes`、`failures`（每部的 ID、标题、错误原因）和更新后的 `catalog`。没有符合条件的电视剧时返回零计数。
近 30 天观看过的电视剧在完整发现同步时会保留在自动片库中，即使不在当次发现集合里。

### 3.10 播放线路管理

这组接口供管理页实时查看并人工固定播放来源。电影按播放页保存；电视剧按剧集 ID 保存一个选择，已收录和后来新增的分集都会重新获取该集的对应来源。HLS 按名称匹配，`url3` 直连按序号匹配，TOS 每集重新获取。某集缺少所选来源时，该集恢复自动探索。会员入口会显示，但因站点验证码要求而不能固定。

人工线路管理当前仅支持 XLYS。LIBVIO 自动选择可读的直连视频，相关人工线路接口返回 `400`；其管理页详情显示“自动选择直连线路”。

#### `GET /api/admin/media/{xlys_id}/playback-routes`

重新读取播放页及 `/lines`，返回实时 HLS、`url3`、TOS 和会员来源及当前播放缓存状态。电视剧使用数据库中的第 1 集列出来源；选择后作用于整部剧。

响应示例：

```json
{
  "page_url": "https://www.xlys02.com/play/27063-0.htm",
  "title": "玩具总动员5",
  "media_kind": "movie",
  "cache_enabled": true,
  "cached_source": "hls",
  "manual_override": true,
  "selected_line": 1,
  "selected_route_name": "iplay",
  "selected_route_key": "iplay",
  "routes": [
    {"line": 0, "key": "inews", "name": "inews", "kind": "m3u8", "selectable": true},
    {"line": 1, "key": "iplay", "name": "iplay", "kind": "m3u8_2", "selectable": true},
    {"line": 2, "key": "url3:0", "name": "直连线路 1", "kind": "url3", "selectable": true},
    {"line": 3, "key": "source:tos", "name": "TOS 直连", "kind": "tos", "selectable": true},
    {"line": 4, "key": "source:member", "name": "会员线路", "kind": "member", "selectable": false}
  ]
}
```

HLS 没有线路名时 `name=null` 且 `selectable=false`。`url3` 只能按站点返回的序号匹配，若上游调整顺序，所选直连可能变化。`cached_source` 也可能是 `tos`、`url3`、`member` 或 `null`；`manual_override=false` 表示它只是自动探索留下的缓存。

#### `PUT /api/admin/media/{xlys_id}/playback-routes`

按 `routes[].key` 人工固定来源。HLS 名称匹配不区分大小写，最终保存上游返回的原始名称；`url3:N` 指定第 N 条直连，`source:tos` 指定 TOS。电视剧会清除已有分集的自动选择缓存，后续逐集匹配；通常管理页的实时 GET 已刷新解析缓存，因此保存本身不再重复等待上游。

```json
{
  "route_name": "iplay"
}
```

成功时返回更新后的 `PlaybackRoutes`。名称不存在返回 `404`，名称不唯一或播放决策缓存关闭返回 `409`。

#### `DELETE /api/admin/media/{xlys_id}/playback-routes`

电影清除该播放页的成功选择和短期失败缓存；电视剧清除整部剧的固定线路以及已收录分集的播放决策缓存。下一次 `/play?source=auto` 会重新并发测速 TOS、member 和 HLS。这个操作只访问本地 SQLite，不等待上游。

```json
{
  "cleared": true,
  "page_url": "https://www.xlys02.com/play/27063-0.htm"
}
```

### 3.11 管理账号

`GET /api/admin/credentials` 返回当前用户名，不返回密码。`PUT /api/admin/credentials` 使用当前账号的 Basic Auth，并提交旧密码、新用户名和新密码：

```json
{
  "current_password": "old-password",
  "username": "media",
  "password": "new-password"
}
```

新密码至少 8 个字符；旧密码错误返回 `403`。更新后，管理 API 与 WebDAV 立即使用新凭据。密码以随机盐和 PBKDF2-SHA256 摘要保存在 SQLite 中。

## 4. WebDAV 接口

WebDAV 是动态生成的虚拟目录，不对应磁盘上的真实 `.strm` 文件。支持的方法只有：

```text
OPTIONS, PROPFIND, GET, HEAD
```

不支持上传、删除、改名、移动或创建目录。

### 4.1 `OPTIONS /dav/` 和 `OPTIONS /dav/{path}`

不要求鉴权。返回 WebDAV 能力头：

```http
DAV: 1
Allow: OPTIONS, PROPFIND, GET, HEAD
MS-Author-Via: DAV
```

### 4.2 `PROPFIND /dav/`

需要 Basic Auth。返回 `207 Multi-Status` XML。

请求头：

| 请求头 | 默认行为 | 说明 |
| --- | --- | --- |
| `Depth` | `1` | `0` 只返回当前资源；其他值按 `1` 处理并包含直接子资源 |

请求体不是必需的，当前实现不会解析 PROPFIND XML 请求体。

根目录包含：

```text
/dav/
├── 电影/
└── 电视剧/
```

### 4.3 电影目录

```text
PROPFIND /dav/电影/
```

返回 SQLite 中可见的电影 `.strm` 文件：

```text
/dav/电影/电影名 (年份).strm
```

如果电影表为空，第一次访问会触发电影资源发现和入库。

### 4.4 电视剧目录

电视剧按以下结构暴露：

```text
/dav/电视剧/
└── 剧名 (年份)/
    └── Season 01/
        ├── 剧名 S01E01.strm
        └── 剧名 S01E02.strm
```

各层目录都支持 `PROPFIND`、`GET` 和 `HEAD`。如果电视剧表为空，第一次访问电视剧根目录会触发电视剧发现和入库。

### 4.5 `GET` 或 `HEAD` `.strm`

`GET` 返回一行本服务 `/play` 地址；`HEAD` 返回相同元数据但没有正文。

响应头包括：

```http
Content-Type: text/plain; charset=utf-8
Content-Length: ...
ETag: "..."
Cache-Control: no-store
```

WebDAV 生成的 STRM 省略 `source`，且播放链路不再接受 line：

```text
/play?page_url=...
```

因此服务端默认执行 `source=auto`，未来调整自动选择策略不需要重新生成 STRM。分片代理模式也不写入 STRM，而是由服务端全局环境变量 `STRM_PROXY_PROXY_SEGMENTS` 决定。

原有兼容文件 `/dav/<STRM_PROXY_DAV_FILENAME>` 仍可直接请求，但不会出现在根目录的 PROPFIND 结果中。

## 5. 常见状态码

| 状态码 | 来源 | 典型原因 |
| --- | --- | --- |
| `200` | 普通 GET、PATCH、POST、OPTIONS | 请求成功 |
| `302` | `/play` | 已选择并重定向到直连 MP4 或本地 HLS |
| `207` | WebDAV PROPFIND | 返回 Multi-Status XML |
| `400` | `/segment` | 分片 URL 不是 HTTPS 或主机不在允许列表 |
| `401` | 管理 API、WebDAV | Basic Auth 缺失或账号密码错误 |
| `404` | 管理策略、WebDAV | 媒体 ID 或虚拟资源不存在 |
| `409` | 管理线路 | 线路名不唯一，或播放决策缓存已关闭 |
| `422` | FastAPI 参数校验 | 缺少必填参数、请求体格式错误或数组长度越界 |
| `502` | 解析器或上游请求 | 上游失败、线路不存在、包装格式变化、找不到 TS 同步包 |
| `503` | `/play?source=auto` | 所有公布的播放来源均探测失败，可按 `Retry-After` 重试 |

错误响应通常为：

```json
{
  "detail": "错误说明"
}
```

## 6. 缓存与请求行为

| 数据 | 默认缓存 | 说明 |
| --- | --- | --- |
| 播放页解析结果 | 300 秒内存缓存 | 包括 PID、标题和候选线路 |
| 解包后的 M3U8 | 300 秒内存缓存 | 缓存键为 `page_url + line` |
| 自动/人工播放决策 | SQLite 持久缓存 | 最近验证成功的 direct source 或命名 HLS 线路及 HLS revision；管理页可覆盖或清除；可关闭 |
| 自动播放失败 | SQLite 30 秒缓存 | 保存各来源错误，抑制播放器短时间内的重复探索；可关闭 |
| 资源发现结果 | 1800 秒内存缓存 | 电影和电视剧分别缓存 |
| SQLite 媒体目录 | 持久化 | 服务重启后仍存在 |
| TS 分片 | 最多 128 MiB 内存 LRU | 代理模式下缓存清洗后的完整 TS，并从当前分片向后预读约 60 秒 |

即使原始 M3U8 命中缓存，服务仍会根据本次请求的 Host 和协议重新生成 `/segment` 地址，因此不同访问地址不会互相污染缓存。

## 7. 日志

`STRM_PROXY_LOG_LEVEL` 支持 `DEBUG`、`INFO`、`WARNING`、`ERROR` 和 `CRITICAL`。默认 `INFO` 适合长期运行；`DEBUG` 适合临时排查上游线路、包装格式和缓存行为。日志同时写入标准输出和 `STRM_PROXY_LOG_FILE`；默认文件为 `data/strm-proxy.log`，达到 10 MiB 后轮转并保留 5 个历史文件。将变量设为空字符串可关闭文件输出。

日志采用便于搜索的 `run=<id> event=<name> key=value` 格式。`run` 在每次进程启动时重新生成，同一轮服务生命周期内保持不变，可用于严格排除重启前和测试进程产生的记录。播放链路常用事件：

| 事件 | 级别 | 含义 |
| --- | --- | --- |
| `play_request` | INFO | TV 请求稳定播放入口及可选 source；不接收 line |
| `play_exploration_started` | INFO | 为该播放页创建视频级探索任务 |
| `play_exploration_joined` | INFO | 并发请求加入已有的视频级探索任务 |
| `play_selection_cache_hit` | INFO | 命中最近验证成功的 source/线路名并映射内部 line |
| `play_selection_cache_set` | INFO | 写入新验证成功的自动播放决策 |
| `play_selection_cache_remapped` | INFO | 按稳定线路名将缓存重新定位到当前 line |
| `play_selection_cache_upgraded` | INFO | 为旧版 HLS 决策补齐并持久化 revision |
| `play_selection_cache_cleared` | INFO | 管理页恢复自动时清除该视频的成功/失败缓存 |
| `play_selection_cache_invalidated` | INFO | 候选变化或缓存方向失败后清除决策 |
| `hls_version_redirected` | INFO | HLS 请求缺少版本或版本过期，重定向到当前 revision |
| `play_failure_cache_hit` | INFO | 在失败冷却期内直接复用失败结果 |
| `play_failure_cache_set` | INFO | 所有来源失败后写入短期失败结果 |
| `lines_parsed` | INFO | `/lines` 动态返回的 HLS 数量、TOS 和会员能力 |
| `direct_source_selected` | INFO | TOS MP4 已验证可用 |
| `hls_probe_start` | INFO | HLS 候选数量、优先顺序和并行探测策略 |
| `hls_line_unhealthy` | INFO | 某条 HLS 首个媒体资源不可用，继续下一条 |
| `hls_line_deferred` | INFO | 健康线路带有包装，暂存为兜底并继续寻找原生线路 |
| `hls_line_probe_error` | WARNING | manifest、嵌套清单或上游请求异常 |
| `hls_line_selected` | INFO | 自动换线最终选择的索引、媒体类型及是否为包装兜底 |
| `play_selected` | INFO | `/play` 最终返回的播放模式和 line/source |
| `segment_upstream_error` | WARNING | `/segment` 请求上游 TS 返回错误状态 |
| `segment_version_redirected` | INFO | 旧 playlist/revision 分片按 index 重定向到当前线路 |
| `segment_request` | DEBUG | TV 的客户端地址、Range、Accept 和 User-Agent（不含敏感头） |
| `segment_upstream_request` | DEBUG | 服务端发往 CDN 的 Referer、Range、Accept 和 User-Agent |
| `segment_upstream_response` | DEBUG | CDN 最终地址、状态、Content-Type、长度和首包等待时间 |
| `segment_sync_not_found` | WARNING | 无法识别 TS，并记录响应元数据及前 16 字节十六进制签名 |
| `segment_prefetch_started` | INFO | 启动一个播放列表的前向预读窗口 |
| `segment_prefetch_finished` | INFO | 前向预读结束及当前缓存条目数、字节数 |
| `segment_prefetch_capacity_reached` | INFO | 当前预读窗口已达到全局容量限制 |
| `segment_prefetch_failed` | WARNING | 后台分片请求或 TS 清洗失败，停止当前窗口 |
| `segment_cache_hit` | DEBUG | 从内存直接返回清洗后的 TS |
| `segment_cache_join` | DEBUG | 相同分片请求加入正在进行的下载 |
| `segment_cache_evicted` | DEBUG | 达到全局上限后按 LRU 淘汰分片 |
| `segment_sync_found` | DEBUG | TS 伪装前缀长度及已检查字节数 |
| `segment_non_ts_discarded` | DEBUG | 丢弃的包装尾部、填充或不完整字节数 |
| `catalog_discovery_complete` | INFO | 电影或电视剧资源发现完成及条目数量 |
| `webdav_list` | INFO | WebDAV 向客户端列出的媒体数量 |

对于“网页可播放、TV 同路线不可播放”，推荐先导出浏览器 DevTools 的 HAR，并保留同一时间段的文件日志。浏览器到 CDN、服务端到 CDN 都使用 HTTPS，`pktmon`/Wireshark 只能用于判断 TCP 重传、吞吐或连接被重置，无法直接读取 HTTP 请求头；HAR 与应用层日志的对照信息通常更有价值。

URL 在写入日志前会移除用户信息、查询参数和 fragment。登录 Cookie、`/lines` 签名、`/god` 表单值和临时媒体 token 均不会主动写入日志。HTTPX 和 HTTPCore 的完整请求 URL 日志固定限制为 `WARNING`；Uvicorn 访问日志保留客户端、方法、路径和状态码，但通过过滤器删除查询串。应用同时输出到标准输出和默认的 `data/strm-proxy.log`，文件按 10 MiB 轮转并保留 5 个历史文件；将 `STRM_PROXY_LOG_FILE` 设为空字符串可关闭文件日志。
