# 从播放页面到 STRM：解析链路说明

本文记录 `strm-proxy` 如何把一个 xlys 播放页面转换成网易爆米花可读取的 `.strm` 内容。示例页面为：

```text
https://www.xlys02.com/play/27062-0.htm
```

文档描述的是当前站点和当前实现，不是通用的 M3U8 爬虫协议。请只处理自己有权访问和播放的内容。

## 最终结果先说明

写进 `.strm` 的不是上游站点返回的临时媒体地址，而是本服务提供的协议无关稳定入口：

```text
http://192.168.1.11:8787/play?page_url=https%3A%2F%2Fwww.xlys02.com%2Fplay%2F27062-0.htm
```

爆米花请求这个 URL 时，服务使用默认 `source=auto`。请求先经过独立的播放决策缓存层：命中最近验证成功的 source 或命名 HLS 路线时直接复用，不再探索其他路径；未命中或缓存失效时固定按 `TOS → member → 探测 HLS` 回退。缓存由 `STRM_PROXY_PLAY_SELECTION_CACHE` 控制且默认开启，持久化在 SQLite 的通用 `cache_entries` 表中。线路名称与当次数字 ID 的映射完全留在服务端，任何 line 都不会固化进 WebDAV 的 `.strm` 或重定向 URL；HLS 重定向只携带服务端生成的 `v` 版本号。

完整数据流如下：

```text
爆米花扫描 WebDAV
  -> GET /dav/痴迷.strm
  -> 得到本地 /play?... 单行 URL
  -> GET 播放页面，提取 pid
  -> 计算动态签名，请求 /lines
  -> 查询播放决策缓存；命名 HLS 路线映射到当次内部 line
  -> 未命中时，按 TOS、member、探测 HLS 依次回退
  -> 相同播放页的并发请求共享一个探索任务
  -> 验证成功后写入播放决策缓存
  -> 全部失败时缓存 30 秒并返回带 Retry-After 的 503
  -> 选定来源后返回 302
  -> HLS 时 302 到带当前 v 的 /hls.m3u8
  -> 旧版或无 v 请求再 302 到当前版本
  -> 当前版本解包成标准 #EXTM3U
  -> HLS 根据全局配置决定直连 TS 或通过 /segment 解包
```

member 的 `ptoken` 只是线路存在标志。站点实际要求识别一张算术验证码，并在同一会话中把计算结果提交到 `/god/{pid}`。当前版本尚未启用自动 OCR，也不提供人工预验证入口；member 会报告未配置，`source=auto` 随后回退到 HLS。

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

## 3. 从 `/lines` 响应识别播放能力

接口成功时返回 `code: 0`，候选线路位于 `data`。当前实现读取两类信息。HLS 候选依次来自：

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

同时还读取：

| 字段 | 含义 | 自动播放行为 |
| --- | --- | --- |
| `tos` | 站点提供 TOS 对象线路 | 调用 `/god/{pid}?type=1`，验证后直连 MP4 |
| `ptoken` | 站点提供会员/验证线路 | 只报告能力；不会把它当验证码提交 |

`/resolve` 的 `sources` 会报告这些能力，但不会把会员 token 或登录 Cookie 返回给客户端。

HLS 线路数量不是配置值，而是上述拆分、过滤和去重后的候选数量，即 `len(resolved.candidates)`，也就是 `/resolve` 响应中 `lines` 数组的长度。候选 URL 末尾可能有 `#maliva`、`#ac5634-us`、`#iplay` 等 fragment；fragment 不会发送给 HTTP 服务器，但实现会保留它作为站点线路标签和稳定线路名。管理页可实时读取这些名称并人工覆盖某个视频的播放决策；缓存命中时按名称在当前候选中重新定位，而不是假设原 line 索引永久不变。

`source=auto` 会先依次验证 TOS 和 member；均不可用时并行探测 HLS 候选线路，每条线路解包 manifest，并以普通流式 GET 读取首个实际媒体资源的少量前缀，任一符合当前传输模式的健康线路先完成即可结束自动选择。这里不使用 Range，因为该分片 CDN 对小 Range 请求反而可能产生显著延迟。播放 API 不接受客户端指定 line；人工选择通过管理页按线路名称写入服务端缓存。

`data.ptoken` 只是会员线路存在标志，不是验证码。网页选择会员线路时会请求 `/play/verifyCode`，显示一张整数加、减、乘算术题，再将用户计算的结果提交给 `/god/{pid}`。当前服务不会把 `ptoken` 作为 `verifyCode`，自动 OCR 仍在验证阶段。

可以通过本地接口查看当前解析结果：

```powershell
$page = [uri]::EscapeDataString('https://www.xlys02.com/play/27062-0.htm')
Invoke-RestMethod "http://127.0.0.1:8787/resolve?page_url=$page"
```

## 4. TOS `/god` 如何变成直连 MP4

TOS 线路不是 M3U8。服务使用与 `/lines` 相同的时间戳签名向 `/god/{pid}?type=1` 发起 POST，并提交网页播放器固定使用的 `verifyCode=888`，取得 JSON 中的对象 URL，再复现网页播放器的 TOS CDN 域名改写。返回给 TV 前会发出一个 `Range: bytes=0-63` 探测请求；只有响应为 `200/206`，并且 Content-Type 是视频或文件头包含 MP4 `ftyp` 时才接受。会员 `/god/{pid}` 使用算术图片验证码而不是 `ptoken`，当前尚未进入自动选择。

当前样本 `24406` 的实测结果为 `206 Partial Content`、`Content-Type: video/mp4`，文件头是 `ftypisom`。不带 Cookie、Referer以及使用普通 TV User-Agent 的 Range 请求均可成功，因此 `/play` 只需返回 `302`，视频数据不经过本服务。

登录环境变量为：

```text
STRM_PROXY_XLYS_USERNAME
STRM_PROXY_XLYS_PASSWORD
```

两者必须同时设置。它们会作为域限定的 `username`、`password` Cookie 发送给 xlys 播放页、`/lines` 和 `/god`，不会发送给媒体 CDN。

## 5. 把包装响应还原为标准 M3U8

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

## 6. 为什么还要代理 TS 分片

当前站点的 `.ts` 响应同样带有伪装前缀，不能保证播放器直接识别。当全局配置 `STRM_PROXY_PROXY_SEGMENTS=true` 时，`/hls.m3u8` 把每个 TS 地址改写为：

```text
http://<本机>:8787/segment?url=<上游TS地址>&referer=<播放页>&playlist=<列表>&index=<序号>&v=<版本>
```

`/segment` 只允许访问配置中的分片域名，避免成为任意 URL 代理。读取上游响应后，它会寻找 MPEG-TS 的同步位置：连续三个 `0x47` 同步字节之间必须各相隔 `188` 字节。找到后丢弃前面的包装数据，从第一个 TS 包开始流式返回，响应类型为 `video/mp2t`。

代理清单还会为每个 TS 附加 HLS revision、内部播放列表标识和分片索引。TV 请求第 N 段后，服务根据 `EXTINF` 从 N+1 开始以最多 2 路并发预读约 600 秒。若 TV 仍拿着旧 playlist 的分片队列，服务会以 `302` 将旧请求重定向到当前 playlist 相同 index 的分片，阻止旧线路继续进入代理。前台未命中的第 N 段仍然边下载边返回，不等待完整分片；传输完成后将清洗后的 TS 放入全局内存 LRU 缓存。默认容量为 128 MiB，达到时间窗口、容量或列表结尾即停止，容量设为 0 可关闭。缓存不落盘，服务重启后清空。

因此这里处理的是站点自定义包装，不是 DRM 解密。本次对示例线路的验证中，解包后的清单没有 `#EXT-X-KEY` 或 `KEYFORMAT`，也没有观察到浏览器 EME/许可证交换；若以后出现这些信号，系统应报告不支持，而不是尝试绕过 DRM。

当 `STRM_PROXY_PROXY_SEGMENTS=false` 时，标准 M3U8 中保留上游 TS 直连地址，TV 不再调用 `/segment`，HLS 自动选线会优先选择 MPEG-TS 从第 0 字节开始或标准 fMP4 的原生线路；若所有健康线路都有 PNG/BMP 等包装，则回退到第一条包装线路。代理开启时，包装和原生线路都能由服务端处理，因此选择首个探测成功的健康线路。切换代理模式需要重启服务，但 STRM URL 不变。

## 7. STRM 内容如何生成

`.strm` 文件本质上是一个只包含播放 URL 的文本文件。本服务的 `/strm` 接口会调用 `build_play_url()`，生成本地自动播放地址：

```powershell
Invoke-WebRequest "http://127.0.0.1:8787/strm?page_url=$page" |
  Select-Object -ExpandProperty Content
```

只读 WebDAV 中的 `痴迷.strm` 使用同一个函数动态生成内容：

```text
PROPFIND /dav/          -> 爆米花发现痴迷.strm
GET /dav/痴迷.strm     -> 返回一行本地 /play URL
GET /play?...           -> 302 到直连 MP4 或带 v、无 line 的本地 /hls.m3u8
GET /hls.m3u8?...       -> 旧 v 先跳转当前 v，再按缓存线路名映射 ID 并返回标准清单
GET /segment?...        -> 代理分片模式下返回去除包装后的 MPEG-TS
```

生成 URL 时使用当前 WebDAV 请求的主机名/IP。因此爆米花通过 `http://192.168.1.11:8787/dav/` 访问时，STRM 内也会得到 `192.168.1.11`，而不是电视或手机无法访问的 `127.0.0.1`。

相关代码位置：

| 阶段 | 模块 |
| --- | --- |
| 页面、签名和线路 | `strm_proxy/xlys.py` |
| M3U8/TS 解包 | `strm_proxy/hls.py` |
| HLS 和分片接口 | `strm_proxy/routes.py` |
| STRM 的 WebDAV 暴露 | `strm_proxy/webdav.py` |

## 8. 缓存和失效行为

页面解析结果和解包后的原始 M3U8 默认在内存中缓存 300 秒；`source=auto` 最近验证成功的播放决策则长期保存在 SQLite。命中命名 HLS 时先按线路名映射到实时 line，再直接复用；命中 TOS/member 时只重新获取并验证该 source，不再探索其他方向。管理页人工固定的线路也存放在这一层，并带有人工覆盖标记；“恢复自动”会仅在本地清除该视频的成功选择和短期失败记录。若缓存直连方向失败，会立即删除并在同一次 `/play` 中执行完整探索；若缓存 HLS 的 manifest 获取失败，则删除缓存供播放器下次请求重新选择。`/god` 的临时媒体 URL 本身不缓存。

探索任务按规范化后的 `page_url` 合并。同一进程中即使多个 TV 请求同时到达，也只抓取和探测一次；某个客户端超时或断开不会取消共享任务。所有来源都失败时，错误明细写入同一张通用 `cache_entries` 表并在 30 秒后过期，`/play` 在此期间直接返回 `503`、`Retry-After: 30` 和机器可读错误码，不生成错误视频。

HLS 决策项优先记录线路名称；每次读取时在实时候选中重新查找该名称并得到当次 line，候选类型、URL 和数字索引只是校验与运行时信息。没有名称的旧缓存才继续严格核对候选类型和 URL。每个 HLS 决策还保存一个 revision，并通过 `v` 下发；人工重选、缓存失效后重新探索会生成新值，相同线路名仅映射到新 line 时保留原值。不带 `v` 或仍请求旧 `v` 的客户端会收到 `302` 跳转到当前版本，因此不会继续命中旧 manifest/分片缓存。版本参数不改变媒体时间轴，通常不会影响媒体库保存的续播进度。缓存中保存的不是带某个本机地址的最终清单；代理开启时，每次返回仍会根据当前请求重新生成 `/segment` URL，所以从 `127.0.0.1` 测试和从局域网 IP 播放不会互相污染。设置 `STRM_PROXY_PLAY_SELECTION_CACHE=false` 可关闭整个决策缓存层；此时仍根据当次候选生成稳定版本，避免重定向循环。

TS 分片缓存是另一层独立的、仅存在于当前进程内的内存缓存。键由上游 URL 和 Referer 共同确定，多部视频共享 `STRM_PROXY_SEGMENT_CACHE_MAX_MB` 指定的全局容量并按 LRU 淘汰；缓存内容始终是已经定位同步字节并对齐到 188 字节的 MPEG-TS。该层只在分片代理开启时工作。

服务端默认直连所有上游。若本机浏览器通过代理访问 CDN，而 Python 服务直连线路明显更慢，可配置 `STRM_PROXY_UPSTREAM_PROXY=http://127.0.0.1:7890`；同一个 HTTP 客户端会将页面解析、manifest、前台 TS 和预读请求全部送入该代理，避免控制面与数据面走不同网络路径。

需要注意：上游的签名算法、包装长度、字段名称和 CDN 域名都属于站点私有实现，未来可能变化。相应适配应限制在 `xlys.py` 和 `hls.py`，不应影响 WebDAV 或 STRM 接口。

## 9. 一条命令验证完整链路

服务启动后运行：

```powershell
.\scripts\verify-webdav-e2e.ps1 -BaseUrl http://192.168.1.11:8787
```

脚本会依次验证 WebDAV 发现、STRM 内容和 `/play` 最终类型。MP4 分支验证 `video/mp4` 与 `ftyp` 文件头；HLS 分支继续验证清单和首个 TS 分片。
