# STRM Proxy 完整数据链路总览

本文用一张图描述当前实现从资源发现、数据库持久化、WebDAV 暴露，到 `/play` 自动选择直连 MP4 或 HLS，以及 MPEG-TS 分片转发的完整链路。

图中的“包装”仅描述代码已经验证的数据布局和处理方式，不对上游采用这种格式的目的作判断。当前实现不解析 PNG 语义：M3U8 按固定前缀长度截取后解压，TS 则通过 MPEG-TS 的同步字节规律寻找真实载荷起点。

```mermaid
flowchart TD
    subgraph A["一、资源发现与建库（控制面）"]
        A1["xlys 列表页 /s/all"] --> A2["抓取最近更新<br/>电影、电视剧各最多 50 条"]
        A1 --> A3["抓取最近 4 年评分列表<br/>电影 &gt; 7.0；电视剧 &gt; 8.0"]
        A2 --> A4["按 xlys_id 合并去重"]
        A3 --> A4
        A4 --> A5{"电视剧是否已知集数"}
        A5 -- "已知" --> A6["推导每集播放页路径"]
        A5 -- "未知" --> A7["访问详情页<br/>提取真实分集链接"]
        A6 --> A8["SQLite<br/>media_items + episodes"]
        A7 --> A8
        A9["管理页面手动导入"] --> A7
    end

    subgraph B["二、通过 WebDAV 暴露虚拟媒体库"]
        B1["TV / 网易爆米花<br/>PROPFIND /dav/"] --> B2["电影/"]
        B1 --> B3["电视剧/"]
        A8 --> B2
        A8 --> B3
        B2 --> B4["电影名 (年份).strm"]
        B3 --> B5["剧名 / Season 01 / S01E01.strm"]
        B4 --> B6["GET .strm"]
        B5 --> B6
        B6 --> B7["返回一行本服务地址<br/>/play?page_url=...<br/>不固化 source"]
    end

    subgraph C["三、/play 自动选择播放协议"]
        B7 --> C1["TV 请求 /play<br/>默认 source=auto"]
        C1 --> CF{"30 秒失败缓存命中？"}
        CF -->|是| CE["503 + Retry-After<br/>返回各来源失败原因"]
        CF -->|否| CS{"该 page_url 已在探索？"}
        CS -->|是| CJ["加入同一个视频级任务<br/>客户端断连不取消任务"]
        CS -->|否| CN["创建视频级探索任务"]
        CN --> C2["共享任务内访问播放页<br/>提取 pid"]
        CJ --> C2
        C2 --> C3["计算时间戳动态签名"]
        C3 --> C4["请求 xlys /lines"]
        C4 --> CC{"播放决策缓存命中？"}
        CM["管理页按 #iplay 等线路名<br/>人工固定或恢复自动"] -.-> CC
        CC -->|HLS 命中| C10["按名称重定位内部 line<br/>302 到带当前 v、无 line 的 /hls.m3u8"]
        CC -->|TOS/member 命中| CR["只重新获取并验证<br/>缓存的 direct source"]
        CR -->|成功| C8["302 到直连媒体 CDN<br/>Cache-Control: no-store"]
        CR -->|失败并清除缓存| C41["优先尝试 TOS<br/>取得并验证直连对象"]
        CC -->|未命中或缓存关闭| C41
        C41 -->|不可用| C42["再尝试 member<br/>当前自动 OCR 尚未启用"]
        C41 -->|成功并缓存方向| C8
        C42 -->|成功并缓存方向| C8
        C42 -->|不可用| C9["并行探测动态 HLS lines<br/>任一健康线路可先返回"]
        C9 -->|缓存成功线路名| C10
        C9 -->|全部失败| CE
    end

    subgraph D["四、媒体数据面"]
        C8 --> D0["TV 直接 Range 请求 MP4 CDN<br/>206 video/mp4"]
        D0 --> D9["TV 解复用并播放音视频"]
        C10 --> DV{"请求 v 是当前版本？"}
        DV -- "否/缺失" --> DR["302 到当前 v<br/>Cache-Control: no-store"]
        DR --> DV
        DV -- "是" --> D1["下载包装后的 M3U8"]
        D1 --> D2["去掉固定前缀并解压<br/>得到标准 #EXTM3U"]
        D2 --> D3{"全局分片代理是否开启"}
        D3 -- "开启" --> D4["清单改写为本服务 /segment<br/>附加 revision + playlist + index"]
        D4 --> DS{"分片仍属于当前 playlist？"}
        DC{"清洗后 TS<br/>内存缓存命中？"}
        DS -- "否" --> DX["按 index 302 到<br/>当前线路对应分片"]
        DX --> DC
        DS -- "是" --> DC
        DC -- "命中" --> D6["直接从内存以 video/mp2t<br/>返回 TV"]
        DC -- "未命中" --> D5["本服务请求上游 TS<br/>寻找 MPEG-TS 同步位置"]
        D5 --> D6
        D4 --> DP["从下一段向后预读约 600 秒<br/>全局最多 2 路并发、128 MiB"]
        DP --> DC
        D6 --> D9
        D3 -- "关闭" --> D7["保留上游 TS 直连地址"]
        D7 --> D8["TV 直接请求带 PNG 前缀的 TS"]
        D8 --> D9
    end
```

## 数据边界

- SQLite 保存媒体元数据、策略、电视剧分集路径，以及通用 `cache_entries` 中的长期播放决策和 30 秒失败结果；不保存影片内容。
- 播放页解析结果和还原后的 M3U8 默认在内存中缓存 5 分钟。
- `source=auto` 最近验证成功的播放方向默认长期保存在 SQLite；命名 HLS 按 URL fragment（如 `#iplay`）在实时候选中重新定位。管理页可以人工覆盖或清除同一条记录；名称消失、变得不唯一或获取失败时删除。
- 同一进程按 `page_url` 合并并发探索；客户端超时或断开不会取消共享任务，探索成功后仍会写入缓存。
- 所有来源失败时 `/play` 返回 `503`、`Retry-After` 和结构化错误，不生成错误视频。
- `/god` 返回的 MP4 地址不写入 SQLite 或 STRM；TOS 每次播放重新获取并验证，随后由 TV 直连 CDN。
- 登录 Cookie 只按域发送给 xlys，不发送给 MP4 或 TS CDN。
- 分片代理开启时，清洗后的 TS 使用全局内存 LRU 缓存；默认从当前播放位置向后预读约 600 秒、总容量最多 128 MiB，服务重启后清空。
- 分片代理关闭时，TV 直接请求上游 TS，本服务不承载视频流量。
- MP4 直连时，TV 使用 Range 请求媒体 CDN，本服务只承担解析和 302 跳转。
- 该过程不转码、不重新压缩，也不修改 MP4/TS 内部的音视频编码。

更详细的签名、线路选择、MP4 探测和包装还原说明参见[《从播放页面到 STRM：解析链路说明》](play-page-to-strm.md)。
