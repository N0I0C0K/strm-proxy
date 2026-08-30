# STRM Proxy API 接口文档

本文记录当前服务实际提供的 HTTP API、管理 API 和只读 WebDAV 接口。默认基础地址为：

```text
http://127.0.0.1:8787
```

部署到其他设备后，将示例中的基础地址替换为服务器地址。交互式 OpenAPI 页面位于 `/docs`，原始 OpenAPI JSON 位于 `/openapi.json`；WebDAV 接口不会出现在 OpenAPI 中。

## 1. 接口与鉴权总览

| 接口组 | 路径 | 鉴权 | 主要用途 |
| --- | --- | --- | --- |
| 播放 API | `/health`、`/resolve`、`/hls.m3u8`、`/strm`、`/segment` | 无 | 健康检查、线路解析和播放 |
| 管理 API | `/api/admin/*` | HTTP Basic | 查询、同步、导入和修改媒体策略 |
| WebDAV | `/dav/` | HTTP Basic，`OPTIONS` 除外 | 向 STRM 播放器暴露虚拟媒体库 |
| 管理界面 | `/admin/` | 页面静态资源无鉴权；API 有 Basic Auth | 浏览器管理媒体库 |

接口行为相关的主要环境变量：

| 环境变量 | 默认值 |
| --- | --- |
| `STRM_PROXY_DAV_USER` | `demo` |
| `STRM_PROXY_DAV_PASSWORD` | `demo` |
| `STRM_PROXY_PROXY_SEGMENTS` | `true` |

公网部署必须修改默认密码并使用 HTTPS 或可信私有网络。当前播放 API 没有鉴权，不应直接无保护地暴露到公网。

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
| `page_url` | string | 是 | — | xlys 播放页 HTTPS URL，应进行 URL 编码 |

`page_url` 只允许配置中的 xlys 域名，并且路径必须符合以下形式之一：

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
  "lines": [
    {
      "index": 0,
      "kind": "m3u8",
      "url": "https://example.invalid/path/video.m3u8"
    }
  ]
}
```

`lines[].index` 是后续 `/hls.m3u8` 和 `/strm` 的 `line` 参数值。线路地址由上游动态返回，不适合长期保存。

### 2.3 `GET /hls.m3u8`

稳定的 HLS 播放入口。服务会解析播放页、请求动态线路、还原包装后的 M3U8，并根据全局环境变量决定是否代理 TS 分片。

查询参数：

| 参数 | 类型 | 必填 | 默认值 | 约束与说明 |
| --- | --- | --- | --- | --- |
| `page_url` | string | 是 | — | xlys 播放页 HTTPS URL，应进行 URL 编码 |
| `line` | integer | 否 | `0` | 非负整数，对应 `/resolve` 返回的线路索引 |

两种分片模式：

| `STRM_PROXY_PROXY_SEGMENTS` | 返回清单中的 TS 地址 | 数据链路 | 兼容性与流量 |
| --- | --- | --- | --- |
| `true` | `http(s)://本服务/segment?...` | TV → 本服务 → 上游 CDN | 兼容性高，全部视频流量经过本服务 |
| `false` | `https://vod.xl01.me/...ts` | TV → 上游 CDN | 本服务流量很小，但播放器必须能识别带 PNG 前缀的 TS |

该配置在服务启动时读取，修改后必须重启。旧客户端 URL 中即使仍有 `proxy_segments=true|false`，也会被当作未知查询参数忽略。`false` 只让 TS 分片直连；M3U8 仍由本服务获取、解包并返回，因为上游原始 M3U8 不是标准明文清单。

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

### 2.4 `GET /strm`

生成适合写入 `.strm` 文件的一行播放 URL。本接口不解析上游播放页，只负责构造 `/hls.m3u8` 地址。

查询参数：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `page_url` | string | 是 | — | xlys 播放页 URL |
| `line` | integer | 否 | `0` | HLS 线路索引，必须大于等于 0 |

响应：

```http
Content-Type: text/plain; charset=utf-8
```

```text
http://127.0.0.1:8787/hls.m3u8?page_url=...&line=0
```

返回 URL 的协议和主机来自当前 HTTP 请求。经过反向代理时，必须正确传递外部 Host 和协议，否则 STRM 中可能出现内网地址或错误的 `http` 协议。

### 2.5 `GET /segment`

获取一个上游 TS 分片，寻找 MPEG-TS 同步位置，丢弃前面的包装字节并流式返回。

查询参数：

| 参数 | 类型 | 必填 | 默认值 | 约束与说明 |
| --- | --- | --- | --- | --- |
| `url` | string | 是 | — | 上游 TS URL；必须是 HTTPS，主机必须等于配置的分片主机 |
| `referer` | string | 否 | `null` | 请求上游分片时使用的 `Referer`，通常是 xlys 播放页 URL |

默认只允许：

```text
https://vod.xl01.me/...
```

响应：

```http
Content-Type: video/mp2t
```

正文从第一个可信 MPEG-TS 同步包开始。服务最多在上游响应的前 64 KiB 中寻找三个间隔 188 字节的 `0x47` 同步字节；未找到时返回 `502`。

该接口不缓存分片，也不先下载完整文件，而是找到 TS 起点后继续流式转发。

### 2.6 `HEAD /segment`

参数与 `GET /segment` 相同。服务向上游发送 `HEAD`，返回上游状态码和允许透传的缓存头，但不读取或验证 TS 内容。

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
| `xlys_id` | integer | 否 | 媒体在来源站点的 ID |
| `title` | string | 否 | 标题 |
| `year` | integer | 是 | 年份 |
| `cover_url` | string | 是 | 封面地址 |
| `douban_rating` | number | 是 | 豆瓣评分 |
| `source_updated_on` | `YYYY-MM-DD` | 是 | 来源更新时间 |
| `policy` | enum | 否 | `auto`、`keep` 或 `hidden` |
| `dav_filename` | string | 否 | WebDAV 文件名或电视剧目录名 |
| `play_page_url` | string | 否 | 电影首集或媒体播放页 URL |
| `kind` | enum | 否 | `movie` 或 `series` |

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
  "recent_limit": 50
}
```

`movies` 字段同样是历史名称，数组中可能包含 `kind=series` 的电视剧。

### 3.3 `GET /api/admin/media`

返回数据库中全部受管理媒体，包括 `hidden` 条目。

响应类型：`MovieCatalog`。

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

同步行为：

1. 抓取电影和电视剧最近更新集合；
2. 抓取最近年份的评分集合；
3. 合并、去重并补抓必要的电视剧详情；
4. 替换不再出现在发现集合中的 `auto` 记录；
5. 保留 `keep` 和 `hidden` 记录。

该请求需要访问上游，可能持续较长时间。响应类型为同步后的 `MovieCatalog`。

### 3.8 `POST /api/admin/import`

从一个 xlys 详情页手动导入电影或电视剧，并将策略设置为 `keep`。

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
| `title` | string | 标题 |
| `year` | integer/null | 年份 |
| `cover_url` | string/null | 封面 URL |
| `available_episode_count` | integer | 当前发现的分集数量 |
| `declared_episode_count` | integer/null | 详情页声明的总集数 |
| `source_url` | string | 规范化后的详情页 URL |
| `message` | string | 导入结果说明 |
| `catalog` | `MovieCatalog`/null | 更新后的媒体目录 |

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

`GET` 返回一行本服务 `/hls.m3u8` 地址；`HEAD` 返回相同元数据但没有正文。

响应头包括：

```http
Content-Type: text/plain; charset=utf-8
Content-Length: ...
ETag: "..."
Cache-Control: no-store
```

WebDAV 生成的 STRM 当前固定使用默认线路参数：

```text
line=0
```

分片代理模式不写入 STRM，而是由服务端全局环境变量 `STRM_PROXY_PROXY_SEGMENTS` 决定。

原有兼容文件 `/dav/<STRM_PROXY_DAV_FILENAME>` 仍可直接请求，但不会出现在根目录的 PROPFIND 结果中。

## 5. 常见状态码

| 状态码 | 来源 | 典型原因 |
| --- | --- | --- |
| `200` | 普通 GET、PATCH、POST、OPTIONS | 请求成功 |
| `207` | WebDAV PROPFIND | 返回 Multi-Status XML |
| `400` | `/segment` | 分片 URL 不是 HTTPS 或主机不在允许列表 |
| `401` | 管理 API、WebDAV | Basic Auth 缺失或账号密码错误 |
| `404` | 管理策略、WebDAV | 媒体 ID 或虚拟资源不存在 |
| `422` | FastAPI 参数校验 | 缺少必填参数、负数 `line`、请求体格式错误或数组长度越界 |
| `502` | 解析器或上游请求 | 上游失败、线路不存在、包装格式变化、找不到 TS 同步包 |

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
| 资源发现结果 | 1800 秒内存缓存 | 电影和电视剧分别缓存 |
| SQLite 媒体目录 | 持久化 | 服务重启后仍存在 |
| TS 分片 | 不缓存 | 每次由 `/segment` 重新访问上游 |

即使原始 M3U8 命中缓存，服务仍会根据本次请求的 Host 和协议重新生成 `/segment` 地址，因此不同访问地址不会互相污染缓存。
