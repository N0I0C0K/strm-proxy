# 从播放页面到 STRM：解析链路说明

本文记录 `strm-proxy` 如何把一个 xlys 播放页面转换成网易爆米花可读取的 `.strm` 内容。示例页面为：

```text
https://www.xlys02.com/play/27062-0.htm
```

文档描述的是当前站点和当前实现，不是通用的 M3U8 爬虫协议。请只处理自己有权访问和播放的内容。

## 最终结果先说明

写进 `.strm` 的不是上游站点返回的临时 M3U8 地址，而是本服务提供的稳定入口：

```text
http://192.168.1.11:8787/hls.m3u8?page_url=https%3A%2F%2Fwww.xlys02.com%2Fplay%2F27062-0.htm&line=0&proxy_segments=true
```

爆米花请求这个 URL 时，服务才去解析播放页、取得当前线路、解包 M3U8，并把媒体分片改写到本地代理。因此上游线路变化时通常不需要重新生成 `.strm` 文件。

完整数据流如下：

```text
爆米花扫描 WebDAV
  -> GET /dav/痴迷.strm
  -> 得到本地 /hls.m3u8?... 单行 URL
  -> GET 播放页面，提取 pid
  -> 计算动态签名，请求 /lines
  -> 选择一条包装过的 M3U8 线路
  -> 去掉包装并解压成标准 #EXTM3U
  -> 把 TS 地址改写为本地 /segment 代理
  -> 去掉 TS 的伪装前缀并流式返回 MPEG-TS
```

## 1. 从播放页面提取标识

服务先请求播放页面，并把站点根地址作为 `Referer`：

```http
GET /play/27062-0.htm HTTP/1.1
Host: www.xlys02.com
Referer: https://www.xlys02.com/
```

页面源码中包含播放器变量。当前实现提取：

- `var pid = ...;`：请求播放线路所需的内容 ID。
- `vod_name = ...`：媒体标题，只用于展示和生成默认文件名。

截至本文验证时，该页面解析为：

```text
pid   = 204486
title = 痴迷
```

对应代码在 `strm_proxy/xlys.py` 的 `parse_page()`。

## 2. 计算 `/lines` 动态签名

播放页本身没有直接给出最终媒体清单。浏览器还会请求站点的 `/lines` 接口。接口参数包括：

| 参数 | 含义 |
| --- | --- |
| `pid` | 从播放页提取的内容 ID |
| `t` | 当前 Unix 毫秒时间戳 |
| `sg` | 根据 `pid` 和 `t` 计算的动态签名 |

签名算法按当前站点 JavaScript 复现：

```text
message    = UTF8("{pid}-{timestamp_ms}")
md5_hex    = MD5(message).hexdigest()
aes_key    = UTF8(md5_hex 的前 16 个字符)
padded     = PKCS#7(message, block_size=16)
signature  = AES-128-ECB(aes_key, padded).hex().upper()
```

然后发出请求：

```http
GET /lines?t=<timestamp_ms>&sg=<signature>&pid=204486 HTTP/1.1
Host: www.xlys02.com
Referer: https://www.xlys02.com/play/27062-0.htm
X-Requested-With: XMLHttpRequest
Accept: application/json, text/javascript, */*; q=0.01
```

签名依赖毫秒时间戳，不能把某次计算的 `sg` 长期写死。实现位于 `strm_proxy/xlys.py` 的 `create_signature()` 和 `_fetch_candidates()`。

## 3. 从 `/lines` 响应选出 M3U8

接口成功时返回 `code: 0`，候选线路位于 `data`。当前实现依次读取：

```text
data.m3u8
data.m3u8_2
data.url3
```

每个字段可能包含逗号分隔的多个地址。服务会：

1. 拆分地址并补全相对 URL。
2. 修正站点曾使用过的旧域名。
3. 只保留包含 `.m3u8` 的项目。
4. 按 URL 去重并保持原顺序。

示例页在本次验证中返回 4 条线路，默认使用 `line=0`。候选 URL 末尾可能有 `#maliva`、`#ac5634-us` 等 fragment；fragment 不会发送给 HTTP 服务器，但实现会保留它作为站点线路标签，以便选择正确的分片基础路径。

可以通过本地接口查看当前解析结果：

```powershell
$page = [uri]::EscapeDataString('https://www.xlys02.com/play/27062-0.htm')
Invoke-RestMethod "http://127.0.0.1:8787/resolve?page_url=$page"
```

## 4. 把包装响应还原为标准 M3U8

选定线路后，请求 M3U8 时会携带播放页作为 `Referer`。发请求前会去掉 URL fragment。

该站点返回的内容不是直接可读的 `#EXTM3U` 文本，而是一个带伪装前缀的压缩载荷。当前样本的还原方式为：

1. 如果响应本来就以 `#EXTM3U` 开头，直接按 UTF-8 读取。
2. 否则去掉前 `3354` 字节包装。
3. 使用 zlib 的 gzip/zlib 自动识别模式解压剩余数据。
4. 确认解压结果以 `#EXTM3U` 开头；否则视为站点格式已经变化。

然后将清单中的相对媒体地址改写成绝对地址。普通线路的 TS 基础地址为：

```text
https://vod.xl01.me/
```

带 `2014-us` 或 `ac5634-us` 标签的线路使用：

```text
https://vod.xl01.me/hls/
```

解包和地址改写位于 `strm_proxy/hls.py`。

## 5. 为什么还要代理 TS 分片

当前站点的 `.ts` 响应同样带有伪装前缀，不能保证播放器直接识别。因此 `/hls.m3u8` 默认把每个 TS 地址改写为：

```text
http://<本机>:8787/segment?url=<上游TS地址>&referer=<播放页>
```

`/segment` 只允许访问配置中的分片域名，避免成为任意 URL 代理。读取上游响应后，它会寻找 MPEG-TS 的同步位置：连续三个 `0x47` 同步字节之间必须各相隔 `188` 字节。找到后丢弃前面的包装数据，从第一个 TS 包开始流式返回，响应类型为 `video/mp2t`。

因此这里处理的是站点自定义包装，不是 DRM 解密。本次对示例线路的验证中，解包后的清单没有 `#EXT-X-KEY` 或 `KEYFORMAT`，也没有观察到浏览器 EME/许可证交换；若以后出现这些信号，系统应报告不支持，而不是尝试绕过 DRM。

## 6. STRM 内容如何生成

`.strm` 文件本质上是一个只包含播放 URL 的文本文件。本服务的 `/strm` 接口会调用 `build_manifest_url()`，生成本地 HLS 地址：

```powershell
Invoke-WebRequest "http://127.0.0.1:8787/strm?page_url=$page" |
  Select-Object -ExpandProperty Content
```

只读 WebDAV 中的 `痴迷.strm` 使用同一个函数动态生成内容：

```text
PROPFIND /dav/          -> 爆米花发现痴迷.strm
GET /dav/痴迷.strm     -> 返回一行本地 /hls.m3u8 URL
GET /hls.m3u8?...       -> 现场解析并返回标准 HLS
GET /segment?...        -> 返回去除包装后的 MPEG-TS
```

生成 URL 时使用当前 WebDAV 请求的主机名/IP。因此爆米花通过 `http://192.168.1.11:8787/dav/` 访问时，STRM 内也会得到 `192.168.1.11`，而不是电视或手机无法访问的 `127.0.0.1`。

相关代码位置：

| 阶段 | 模块 |
| --- | --- |
| 页面、签名和线路 | `strm_proxy/xlys.py` |
| M3U8/TS 解包 | `strm_proxy/hls.py` |
| HLS 和分片接口 | `strm_proxy/routes.py` |
| STRM 的 WebDAV 暴露 | `strm_proxy/webdav.py` |

## 7. 缓存和失效行为

页面解析结果和解包后的原始 M3U8 默认缓存 300 秒。缓存可以减少爆米花扫描、媒体探测和正式播放连续触发的上游请求。

缓存中保存的是上游解析结果，不是带某个本机地址的最终清单。每次返回时仍会根据当前请求重新生成 `/segment` URL，所以从 `127.0.0.1` 测试和从局域网 IP 播放不会互相污染。

需要注意：上游的签名算法、包装长度、字段名称和 CDN 域名都属于站点私有实现，未来可能变化。相应适配应限制在 `xlys.py` 和 `hls.py`，不应影响 WebDAV 或 STRM 接口。

## 8. 一条命令验证完整链路

服务启动后运行：

```powershell
.\scripts\verify-webdav-e2e.ps1 -BaseUrl http://192.168.1.11:8787
```

脚本会依次验证 WebDAV 发现、STRM 内容、HLS 头和首个 MPEG-TS 分片。成功时首个分片应以 `0x47` 开始。
