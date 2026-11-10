# R15 媒体加速：nginx X-Accel-Redirect 方案（v4 审核修订草案）

> 状态：v4 审核修订草案，未实施、未转 OpenSpec。本文只定义目标架构、边界、配置与验收，不修改代码、镜像、compose 或部署。
> 日期：2026-11-10
> 关联：`用户与个人空间设计方案.md` §6.5、R15；`用户空间验收矩阵.md` R15；`api/routers/media.py`；`storage/minio.py`。

## 0. 修订沿革与 v4 摘要

v1 方向正确但不能直接实施；v2 修 P0 路由/签名；v3 补 SSE/网络边界与固定 upstream；v4 收敛 nginx 配置，避免重复 location、限流/真实 IP/日志作用域冲突。当前有效要求以本文 v4 正文为准。

1. **覆盖 saved-media**：`/api/space/saved/{saved_id}/media` 是个人空间一次渲染大量 `<video>` 的主要压力源，必须单独由 edge-nginx 直连后端，不能继续走 Next。
2. **不再使用 `$arg_target` 动态 URL**：改为固定 MinIO upstream + 后端生成的 presign query；避免 nginx `$arg_*` 不解码/`&` 拆参数的问题。
3. **HEAD 不走 GET presign**：HEAD 继续走现有后端 `stat`/Range 头逻辑，不返回 X-Accel；只有 GET 加速。
4. **响应头由 internal location 重建**：不假设 X-Accel-Redirect 会保留后端 `Content-Type`/`Content-Disposition`/`Cache-Control` 等头。
5. **网络隔离成为硬前提**：backend/frontend 端口都不得对浏览器/公网开放；`X-VC-Accel` 只是二次确认，不是信任边界。
6. **失败不承诺自动回退**：X-Accel 发出后内部代理 404/502/504 由 nginx/客户端处理；自动回退需单独的 nginx error_page 设计和防循环标记。
7. **R15 有界迁到 nginx**：增加 `limit_conn`/`limit_req` 近似闸门和 nginx 侧指标；明确标准 nginx 的 `limit_conn` 不等价于 HTTP/2 并发流上限，严格 8 流需 OpenResty/njs 或保留后端流式。
8. **修正 presign、配置、日志、超时、bucket、Phase 1 价值等 P1 问题**。
9. **MinIO 签名保真**：内层 query 只放 presign 原串，不追加 `download`/`filename`/`file_id`；object key 限 ASCII 安全集；用显式 `proxy_pass ...$uri$is_args$args`，越界 key 回退流式。

## 1. 现状与问题

当前媒体链路：

```text
浏览器
  -> SLB / Next basePath
  -> /code/... 或 /api/space/saved/.../media
  -> FastAPI media router
  -> 本地文件或 MinIO
  -> 后端逐块转发给浏览器
```

现状事实：

- `BASE_PATH=/Bocom_Video_Generation`；后端容器内端口来自 `${BACKEND_PORT}`（当前 `.env.example` 为 8500，不能写死在方案里）；
- 后端 `/code/...` 媒体路由在 `api/routers/media.py`；
- `saved-media` 路由是 `/api/space/saved/{saved_id}/media`，当前经 Next 的 `/api/space/:path*` rewrite；
- 生产新写入强制 MinIO，local 主要服务 legacy 行；
- MinIO 的 `_public_client` 是给推理服务 presign 用的，不是浏览器直连 endpoint；
- 后端 `media_metrics` 只统计经过后端的流；
- 现有 R15 已有限并发流、Range/HEAD/416、协程级 300s 超时、断开/partial/timed_out 指标。

X-Accel 的目标是：**后端只做控制面（owner、DB、Range、missing），把字节传输交给 nginx**；不改变 `?user_login=` accepted bypass 口径。

## 2. 目标与非目标

### 目标

1. 覆盖 `/code/...` 与 `/api/space/saved/*/media` 两个媒体通道。
2. 本地 legacy 文件由 nginx `alias` 读取。
3. MinIO 新产物由 nginx 从内部 MinIO 读取，不把 MinIO/bucket/presign 暴露给浏览器。
4. GET 支持 Range 206、非法/多段 Range 416；HEAD 保持后端 stat 路径。
5. 保留 `Content-Type`、`Content-Disposition`、`Cache-Control: private, no-store`、`Referrer-Policy: no-referrer`、`X-Content-Type-Options: nosniff`、`Accept-Ranges`。
6. 保留现有后端流式路径，作为未启用、无 edge secret 或回滚时的路径。
7. nginx 侧有独立的并发/排队/错误指标和脱敏日志。

### 非目标

- 不改变媒体 query 自报身份风险；改 `user_login` 仍可冒读，属于 accepted risk。
- 不把 MinIO bucket 设为匿名公开。
- 不把 presign URL 返回浏览器。
- 不把 X11 写成“自动无缝回退”；自动回退需要单独设计。
- 不在本方案中修改 compose/端口；这些变更必须另获批准。
- 不解决多实例 `_refs` 跨进程和 R15 之外的多用户配额。

## 3. 决策摘要

| 决策 | 采用 | 理由 |
|---|---|---|
| 边缘组件 | edge-nginx 在 SLB 与 Next 之间 | 只有 nginx 能消费 X-Accel；Next rewrite 会透传头 |
| `/code/` 路由 | edge-nginx 直接到后端 | 不能经过 Next rewrite |
| saved-media | 只匹配 `/api/space/saved/<id>/media` 的正则 location 直达后端 | 个人空间视频是主要压力源；不能把 DELETE/列表等非媒体端点一并捕获 |
| 本地文件 | internal alias | GET 由 nginx 处理 Range；HEAD 仍回后端 |
| MinIO | 固定 upstream `minio:9000` + presign query | 避免 `$arg_target` 不解码/SSRF/开放代理 |
| HEAD | 后端 stat，不发 X-Accel | GET presign 不能用于 HEAD；保留现有 200/416 语义 |
| 回退 | 未启用/无 secret 时后端流式；X-Accel 失败明确返回 | 发出 X-Accel 后后端无法接管字节 |
| 并发闸门 | nginx `limit_conn`/`limit_req` 近似；严格 8 并发需 OpenResty/njs 或保留后端流式 | 标准 nginx 的 `limit_conn` 不等价于 HTTP/2 并发流上限 |
| 信任边界 | backend/frontend 端口网络隔离 + `X-VC-Accel` secret | header 不是身份边界 |

### 3.1 为什么不继续用 `$arg_target`

v1 的：

```nginx
set $vc_target $arg_target;
proxy_pass $vc_target;
```

在 nginx 原生配置中不可用：`$arg_*` 不做 URL 解码；presign URL 含多个 `X-Amz-*` 参数，整体 urlencode 后正则不匹配，不编码则 `&` 被拆成多个参数。v2/v3 改为：

```text
X-Accel-Redirect: /__vc_media/minio/<bucket>/<key>?<presign-query>
```

nginx 的 internal location 只代理**固定** `minio:9000`，原始 query 由 nginx 作为 args 转发；不需要 njs/lua，也不接受用户提供 URL。

## 4. 部署拓扑

```text
客户端
  -> SLB
     -> edge-nginx
        ├─ /Bocom_Video_Generation/                         -> Next:3000
        ├─ /Bocom_Video_Generation/api/space/saved/<id>/media -> backend:8500（仅该正则；模板变量）
        ├─ /Bocom_Video_Generation/code/                    -> backend:8500（直连；模板变量）
        ├─ /Bocom_Video_Generation/api/*（其余，含 SSE）      -> Next:3000
        └─ internal /__vc_media/...                         -> local alias / minio:9000
```

### 4.1 路径与前缀

```nginx
# ===== http 级（这些指令只能放 http）=====
limit_conn_zone $server_name zone=vc_media_conn:10m;
limit_conn_zone $binary_remote_addr zone=vc_media_ip:10m;
limit_req_zone $binary_remote_addr zone=vc_media_req:10m rate=100r/s;
log_format media '$remote_addr $request_method $uri $status '
                 '$body_bytes_sent $request_time $upstream_response_time';
map $arg_download $vc_local_disposition { default "inline"; "1" "attachment"; }

# ===== server 级（可放在实际 server{} 内）=====
set_real_ip_from <SLB_CIDR>;   # 模板渲染；要求 SLB 覆盖客户端 XFF
real_ip_header X-Forwarded-For;
real_ip_recursive off;         # 或 on + 明确 SLB 覆盖语义
set $vc_accel_secret "<shared-secret>";   # 由部署 include 渲染，不允许裸注释
limit_conn_status 503;
limit_req_status 503;
access_log /var/log/nginx/media.log media;  # 唯一一处；location 不得覆盖

# ===== 以下每个 location 只定义一次 =====
# saved media 只匹配 .../<saved_id>/media；不能捕获 DELETE 或列表端点
location ~ ^/Bocom_Video_Generation/api/space/saved/([^/]+)/media$ {
    limit_conn vc_media_conn 8;
    limit_conn vc_media_ip 8;
    limit_req zone=vc_media_req burst=200 nodelay;  # 初始值，必须压测标定
    rewrite ^/Bocom_Video_Generation(/api/space/saved/.*)$ $1 break;
    proxy_pass http://backend:8500;   # 端口由部署模板渲染为 ${BACKEND_PORT}
    proxy_http_version 1.1;
    proxy_buffer_size 16k;             # X-Accel header 含 presign query，留余量
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-VC-Accel $vc_accel_secret;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 30s;   # 只用于鉴权/返回 X-Accel，不是字节传输
}

location ^~ /Bocom_Video_Generation/code/ {
    limit_conn vc_media_conn 8;
    limit_conn vc_media_ip 8;
    limit_req zone=vc_media_req burst=200 nodelay;
    proxy_pass http://backend:8500/code/;
    proxy_http_version 1.1;
    proxy_buffer_size 16k;             # X-Accel header 含 presign query，留余量
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-VC-Accel $vc_accel_secret;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 30s;
}

# 其余 API 和 SSE 经 Next；SSE 必须关闭缓冲并使用长读超时
location /Bocom_Video_Generation/api/ {
    proxy_pass http://frontend:3000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 10800s;
    proxy_send_timeout 10800s;
}

location /Bocom_Video_Generation/ {
    proxy_pass http://frontend:3000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Connection "";
}
```

要点：

- saved regex location 不能使用 `^~` 前缀；否则会连同 DELETE/列表一起捕获。`/api/` 泛 location 也不能用 `^~`，否则 regex 不会执行。
- `proxy_pass` 末尾路径按后端真实路由写；saved 路由保留 `/api/space/saved/<id>/media`。
- 根路径部署时增加不带 basePath 的等价 location，或将 basePath 变量化。
- SSE 不是“假设需要直连”：当前前端就是经 Next rewrite 同源访问，edge 必须为 `/api/*` 提供 `proxy_buffering off` 和覆盖生成时长的 `proxy_read_timeout`。
- backend/frontend 端口来自 `${BACKEND_PORT}` / 前端端口；网络隔离见 §9.1，移除宿主 ports 是部署变更，需单独审批。
- `proxy_pass` 使用字面量 `backend`/`frontend`，nginx 启动时解析；部署时必须让 edge-nginx `depends_on` 这两个服务，或在启动前完成 DNS/服务就绪检查。若改为变量形式，需按 MinIO location 配 `resolver`。

### 4.2 X-VC-Accel 注入

```nginx
# $vc_accel_secret 已在 §4.1 的 server 级定义；这里只引用，不重复 set
proxy_set_header X-VC-Accel $vc_accel_secret;
```

不要使用 `$http_x_vc_accel` 透传。`X-VC-Accel` 只作为二次确认；真正的边界是网络隔离。后端在没有可信头时走现有 StreamingResponse，不返回 X-Accel。

## 5. 后端控制面设计

> 以下为方案，不是当前已实现代码。

### 5.1 开关与配置

配置通过现有设置层提供，不在路由里散落 `os.getenv`：

```text
VC_MEDIA_ACCEL_ENABLED=false
VC_MEDIA_ACCEL_EDGE_SECRET=<shared-secret>
VC_MEDIA_ACCEL_MINIO_TTL_SECONDS=60
VC_MEDIA_ACCEL_LOCAL_ROOT=settings.RESULT_DIR
```

- `LOCAL_ROOT` 取 `settings.RESULT_DIR`，不硬编码 `/app/code/result`。
- bucket 从 `settings.STORAGE_BUCKET` / `settings.STORAGE_SAVED_BUCKET` 读取，不硬编码 `videoclaw`。
- 如果项目设置层没有 media 节，应先在 `config.py` 增加字段；不要复制现有 `VC_MEDIA_MAX_STREAMS` 的直接 `os.getenv` 风格。

### 5.2 请求流程

对 `/code/...` 和 saved-media 的 GET：

1. 解析 `?user_login=`，执行现有 owner/DB/规则表校验。
2. 对 MinIO 做 `stat`；缺失则 `mark_missing`、404，不返回 X-Accel。
3. 解析 Range：
   - 合法单段：继续；
   - 多段/非法/越界：返回 416，不返回 X-Accel；
   - HEAD：返回 200/416 头，不返回 X-Accel。
4. 构造内部 URI：
   - local：`/__vc_media/local/<relative_after_/code/result/>?download=...&filename=<urlencoded-basename>`
   - MinIO：`/__vc_media/minio/<bucket>/<key>?<presign-query>`
   - MinIO 的 query **必须逐字节等于 presign 返回的 query**，不得追加 `download`/`filename`/`file_id`；download 语义走已签名的 `response-content-disposition`。
5. 仅在 `enabled=true` 且 `X-VC-Accel` 匹配时返回 `X-Accel-Redirect`。
6. 否则走现有 `StreamingResponse`。

### 5.3 HEAD 与 Range

- HEAD 永远走当前后端 stat 分支，返回 `Content-Length`、`Content-Type`、`Accept-Ranges` 等；**不产生 X-Accel**。
- GET 的合法 Range 由后端预校验，nginx 负责实际 206；后端不再读取字节。
- 非法/多段 Range 由后端返回 416，保证与现有行为一致。
- nginx 内部 location 设置 `max_ranges 1`，并透传 `Range`/`If-Range`。

### 5.4 响应头

X-Accel 会丢弃后端响应体/响应的默认转发语义；internal location 必须显式重建。具体指令只写在 §6.1/§6.2 各一次，本节不重复粘贴，避免重复响应头：

- local：重建 `Cache-Control`、`Referrer-Policy`、`X-Content-Type-Options`、`Accept-Ranges`、`Content-Disposition`；
- MinIO：隐藏上游 `Cache-Control`/`Expires`，保留由 presign 的 `response-content-type` / `response-content-disposition` 生成的 `Content-Type`/`Content-Disposition`；
- 两处都不得重复 `add_header Content-Disposition`/`Accept-Ranges`。

local alias 不经过 presign，必须单独处理：

- 优先使用 `types`/`default_type` 映射扩展名；
- 后端预校验 DB `media_type` 与映射一致；不一致时回退后端流式（X-Accel 不发出）；
- `Content-Disposition` 使用与 MinIO 相同的 RFC5987 格式，只接受后端校验过的 basename。

## 6. nginx internal location

### 6.1 本地 alias

```nginx
# map 已在 §4.1 的 http 级定义，这里不要重复
location ^~ /__vc_media/local/ {
    internal;
    alias /srv/videoclaw/result/;
    max_ranges 1;
    default_type application/octet-stream;
    types {
        image/png png;
        image/jpeg jpg jpeg;
        image/webp webp;
        video/mp4 mp4;
        video/webm webm;
        application/octet-stream bin;
    }
    # types 表必须与实际产物类型对齐；未覆盖扩展名或 DB media_type
    # 不一致时 backend 回退流式，不把 Content-Type 交给猜测
    add_header Cache-Control "private, no-store" always;
    add_header Referrer-Policy "no-referrer" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Accept-Ranges "bytes" always;
    # filename 由后端校验为 basename 并 URL 编码；不可直接使用任意 query
    add_header Content-Disposition "$vc_local_disposition; filename*=UTF-8''$arg_filename" always;
}
```

路径映射：

```text
/code/result/image/a.png
  -> /__vc_media/local/image/a.png
  -> alias /srv/videoclaw/result/image/a.png
```

后端生成前必须：

- `urllib.parse.unquote` 一次；
- `posixpath.normpath`；
- 拒绝绝对路径、`..`、`%2f`/`%2e%2e`、空段、双斜杠；
- 只允许来自 DB `public_path` 或 legacy 规则表的路径。

不使用 `merge_slashes off`（该指令只能在 http/server 级，且会改变全局 URI 规范化）；由后端 key 约束 + 双侧编码规范化负向测试保证，含 `//`/`..` 的内层 URI 不进入 X-Accel。

### 6.2 MinIO 固定 upstream

```nginx
location ^~ /__vc_media/minio/ {
    internal;
    # 127.0.0.11 仅适用于 compose embedded DNS；独立 nginx 改部署平台 resolver
    resolver 127.0.0.11 valid=30s;
    # 不设置 merge_slashes；由后端 key 约束保证没有 //、.、..
    # nginx URI 已经过解码；后端只允许 ASCII 安全 key，越界时回退流式
    rewrite ^/__vc_media/minio(/.*)$ $1 break;
    proxy_pass http://minio:9000$uri$is_args$args;
    proxy_set_header Host minio:9000;   # 必须与 settings.STORAGE_ENDPOINT 同一部署模板
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
    proxy_next_upstream off;
    max_ranges 1;

    # Content-Disposition/Content-Type 由内部 presign 的 response-* 参数固定，
    # 不要隐藏它们；只隐藏可能叠加的缓存头。
    proxy_hide_header Cache-Control;
    proxy_hide_header Expires;
    add_header Cache-Control "private, no-store" always;
    add_header Referrer-Policy "no-referrer" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Accept-Ranges "bytes" always;
}
```

内部 URI 的 path/query 由后端从**内部 presign URL** 原样拆分：

```text
X-Accel-Redirect:
  /__vc_media/minio/<bucket>/<ascii-safe-object-key>?<presign-query>
```

- 不使用 `$arg_target`，不接受任意 URL；
- bucket 必须属于配置白名单；`saved` 使用 saved bucket；
- object key 必须满足 `[A-Za-z0-9_./-]+`，且不得有 `.`/`..` 段或连续 `//`；含空格、中文、`%2F`、`%2e%2e` 的 key 不进入 X-Accel，回退后端流式；
- path 与 query 必须与 presign 返回值逐字节一致；`download`/`filename`/`file_id` 不得追加到 MinIO query；
- presign query 原样转发，由 MinIO 校验 SigV4；
- 不在 nginx 记录完整 query；
- 验收时用 `tcpdump` 或临时抓包对照 nginx 实际送到 MinIO 的 URI 与后端生成值。

### 6.3 nginx 并发闸门

`limit_conn_zone`、`limit_req_zone`、`log_format` **只在 §4.1 的 http 级定义一次**；`limit_*_status`、`set_real_ip_*`、`access_log` 在 server 级。对应 `limit_conn`/`limit_req` 已合并进 §4.1 的 saved 与 code location，不能重复粘贴 location（否则 `duplicate location` 或正则静默失效）。

必须写清语义：

- `limit_conn` 限制的是连接；HTTP/2 多路复用下，100 个 `<video>` 可能只占 1 个连接，不能当成 8 个并发流。
- `limit_req` 限制请求速率，不等价于“同时活跃媒体流”。
- `rate=100r/s`、`burst=200` 是**待压测标定的初始值**；X12 必须记录 100 个 preload 的 503 数量，不能同时承诺“全部成功”和“固定限流”。
- 若必须保持 R15 的 8 流硬上限，需 OpenResty/njs 共享计数，或**不要启用 X-Accel** 并继续走带 Semaphore 的后端流式；不能宣称标准 nginx 已等价迁移。
- `limit_conn_status 503`/`limit_req_status 503` 需要单独的 503 页面/重试策略，不能与鉴权 401/404 混淆。
- X-Accel 内部重定向是否重复计入外层 `limit_conn`/`limit_req` 需实测，列入 Phase 0。

## 7. MinIO 内部 presign

### 7.1 client 选择

当前 MinIO 的 `_public_client` 是**推理服务**访问 MinIO 的 public endpoint，不是浏览器 endpoint；`public_endpoint == endpoint` 时它可能与主 client 是同一个对象，不能靠对象身份判断“内部/外部”。v3 改为：

- `internal_presign_client`：按 `settings.STORAGE_ENDPOINT` 显式构造，仅用于 nginx 内部代理；
- `public_client`：继续只供推理服务 presign；
- 不允许把 `public_client` 的 URL 给 nginx 或浏览器；
- endpoint 与 client 变量分开配置，不根据 client 对象是否相同做分支。

### 7.2 TTL 与响应头

- TTL 60s，仅覆盖 nginx 收到 X-Accel 到开始请求 MinIO 的窗口；
- 连接建立后 SigV4 不再重新校验 TTL；
- 扩展 `storage/minio.py` 的 `presign()` 支持 `response-content-type`；
- 统一 RFC5987 的 `Content-Disposition` helper：`inline/attachment; filename*=UTF-8''<encoded-basename>`；local 与 MinIO 使用同一 helper；
- MinIO 通过 `response-content-disposition` 固定；local 通过 internal location 的安全参数设置；
- `download`/`filename` 不得作为**未签名**参数追加到 MinIO 的 presign query；`response-content-disposition` 本身是 presign query 的已签名参数。
- local 内部 URI 可以使用 `download`/`filename`，但必须由后端校验 basename 后生成。
- filename 只允许 basename，不能直接回显原始用户输入。

### 7.3 HEAD 不 presign

MinIO SDK 的 `presigned_get_object` 是 GET 签名。HEAD 即使能返回头，也可能因方法不匹配得到 403。v3 明确：

- HEAD 永远走后端 `stat`，不产生 X-Accel；
- 只有 GET 走内部 presign；
- 这是有意的功能/性能取舍，不把 HEAD 标成“已加速”。

## 8. 失败、missing 与回退

### 8.1 失败语义

- 后端预校验缺失：`mark_missing`、404，不产生 X-Accel。
- X-Accel 发出后，本地 alias 404 / MinIO 404：客户端收到 404；不能保证后端已写 `missing_at`。
- 内部代理 502/504：返回明确 502/504（可带 `Retry-After`），客户端重试；不宣称“自动回退到流式”。
- 若需要真正回退，必须增加 nginx `error_page` → 专用后端 fallback 端点，并带 `X-VC-Accel: off` 防止循环；这是 Phase 2 之后的独立设计，不是当前默认行为。

### 8.2 missing 回调：本轮不做

v2/v3 曾建议用 `error_page` 回调后端标记 `missing_at`，该设计不成立：

- named location 的响应会替换原始 404/502/504，无法同时保留原响应；
- 内部重定向后没有可靠的 `$arg_file_id`，把 `file_id` 塞进 MinIO query 又会破坏 SigV4；
- 原生 nginx 没有“返回原响应 + 异步副作用”的可靠语义。

因此本轮明确：

- 只在 X-Accel 发出前的 `stat` 阶段维护 `missing_at`；
- 预校验后对象消失的 404/502/504 只返回给客户端，不更新 DB；
- 验收表把该漂移写成已知边界，不以错误回调掩盖；
- 若以后必须维护，需设计独立 sidecar/消息队列，不在 nginx error_page 中实现。

## 9. 安全与网络隔离

### 9.1 硬前提

1. 后端与 frontend 都不得对浏览器/公网开放；移除两者的宿主 `ports`（或至少只绑定 loopback），保留 compose `expose`，允许同一网络内的 frontend 与 edge-nginx 访问。
2. 当前 SSE 经 Next rewrite 同源访问；edge-nginx 必须为 `/api/*` 提供不缓冲、长读超时配置，不能假设浏览器直连后端。
3. `X-VC-Accel` secret 只作二次确认，不能替代网络 ACL。
4. internal location 必须 `internal`；公网访问返回 404/403。
5. 内部 presign 不返回浏览器；X-Accel 头在 edge 终止。
6. 直连后端伪造 `X-VC-Accel` 必须被网络隔离阻断；补负向测试。

### 9.2 保留的风险

- `?user_login=` 改参仍可冒读，用户已接受；
- API 自报头仍是 accepted risk；
- nginx 日志含 query 时必须脱敏；
- presign 出现在后端→nginx 的 header 中，任何 header 日志/中间层都不能落盘。

## 10. 指标与日志

### 10.1 后端

`/api/health.media_streams` 只统计**后端流式回退**路径：

- `fallback_requests`；
- `fallback_active`；
- `fallback_completed/failed/disconnected/partial/timed_out`；
- `fallback_bytes`。

X-Accel 命中不计入这些字段，避免“全 0 看起来没有流量”的误读。

### 10.2 nginx

`log_format media` 与 `access_log` **只在 §4.1 定义一次**；本节只引用，不重复定义：

- 日志字段使用 `$uri`，不含原始 `$request`，避免 `?user_login=` 和 presign query 落盘；
- `access_log` 必须在 server 级统一，location 不得覆盖。

增加：

- `X-Accel` 命中/回退；
- 内部 upstream 404/502/504；
- `limit_conn`/`limit_req` 拒绝数；
- 每路径（`/code/`、`/api/space/saved/`）的请求量/字节量；
- `access_log` 必须在 server 级统一使用 `media` 格式，location 不得覆盖。

可选用 `stub_status` 或日志聚合；不把 nginx 指标伪装成后端 `media_streams`。

## 11. 分阶段实施

### Phase 0：网络与配置前置

- 关闭后端与 frontend 的宿主端口，edge-nginx 成为唯一入口；
- 确认 SSE/API 路径；
- 生成 secret include，配置日志脱敏；
- `nginx -t` 必须由部署侧提供的既有 nginx 环境执行；若当前项目没有 nginx 服务，只做配置静态评审，不为验证新建镜像/另起测试环境；
- 后端回归仍只在既有 `video-claw-backend` 容器内执行；
- 增加 SSE 长连不断流阻断验收（`/api/*` 经 edge/Next）。
- 不修改业务代码。

### Phase 1：控制面与 saved-media 直连

- edge-nginx 增加只匹配 `.../<saved_id>/media` 的 saved-media 正则 location；
- 后端仅做 owner/Range/HEAD 预校验，暂不返回 X-Accel；
- 验证 saved 页 100 个 `<video preload="metadata">` 的 HEAD/Range 仍正常；
- 这一阶段不改变字节路径，但验证路由拆分与网络隔离。

### Phase 2：本地 legacy alias

- 只对 `storage_kind=local` 返回内部 alias；
- 验证 Range/416/HEAD/download/路径穿越；
- 明确这是兼容性验证，不把本地 legacy 当作生产 R15 收益。

### Phase 3：MinIO 固定 upstream presign

- 后端 internal client + presign；
- nginx 固定 upstream + Range/HEAD（HEAD 仍后端）；
- 真实并发、慢客户端、断线、MinIO 失败；
- 不实现 missing 回调，验收表记录“预校验后对象消失可能不更新 missing_at”。
- 生产单 bucket/路径灰度。

### Phase 4：默认开启与回滚

- 默认 `enabled=true`；
- 演练 `enabled=false`、无 secret、内部 502/504、MinIO 不可用；
- 回滚后现有后端流式 200/206/HEAD 不受影响。

## 12. 验收矩阵

| 编号 | 场景 | 预期 |
|---|---|---|
| X1 | owner 匹配的本地文件 | nginx alias 200；后端不转发字节 |
| X2 | 无/非法身份 | 401；不产生 X-Accel |
| X3 | 他人文件 | 404；不产生 X-Accel |
| X4 | 公网访问 internal location | 404/403 |
| X4b | 直连后端并伪造 X-VC-Accel | 由网络 ACL 拒绝；若可达则失败并告警 |
| X4c | 伪造 X-Forwarded-For | SLB 必须覆盖/规范化 XFF；不得让 `limit_req` 被任意 IP 绕过 |
| X5 | 合法单段 Range | 206 + `Content-Range` + `Accept-Ranges` |
| X6 | 非法/多段/越界 Range | 416；不产生 X-Accel |
| X7 | HEAD | 后端 200 + 正确长度，无 body；不依赖 presign |
| X8 | download=1 | attachment + basename；`Content-Disposition`/`Accept-Ranges` 无重复头 |
| X9 | MinIO owner 匹配 GET | nginx 从固定 upstream 读取 200；MinIO 收到的 query 与后端 presign 值逐字节一致 |
| X10 | MinIO 对象缺失（预校验） | 404 + `missing_at` |
| X11 | 预校验后对象消失/内部 502/504 | 明确 404/502/504；不宣称自动回退；`missing_at` 可能不更新 |
| X12 | saved-media 100 个 preload | saved 正则 location 命中；DELETE/列表不经该 location；记录 503 数量与 `rate/burst` 标定值，不承诺在固定限流下全部成功 |
| X12b | 持续 >100r/s 或多用户叠加 | 达到 `limit_req` 边界并记录 503，验证标定值不是永远不触发 |
| X13 | 慢客户端/慢 MinIO | nginx `proxy_read_timeout` 生效；有 nginx 指标 |
| X14 | 无 X-VC-Accel 直连 | 后端 StreamingResponse 回退 |
| X15 | 同源 `/Bocom.../code`/saved | 命中 edge；Next 不参与媒体字节 |
| X16 | 日志 | server 级 media 格式，不含完整 login/presign query |
| X17 | 并发超限（>8 连接或 >200 burst） | nginx 503；后端 media_streams 不误报 |
| X18 | SSE 长连接经 edge/Next | 不因 `proxy_buffering`/60s 默认超时断流 |
| X19 | MinIO path/query 保真 | 抓包值与后端 presign 值逐字节一致；覆盖最长 object key + 最长 RFC5987 basename |
| X20 | 非 ASCII/空格/`%2F` key | 不进入 X-Accel，回退后端流式 |

## 13. 风险与待决策

| 风险 | 处理 |
|---|---|
| 后端/frontend 端口暴露 | 网络隔离；未隔离不启用 X-Accel |
| saved-media 经 Next | 仅 `.../<id>/media` 正则 location；否则不宣称 R15 收益 |
| presign 在 header 中泄露 | 网络 ACL + 不记录 header；必要时 opaque token |
| MinIO presign query 被 nginx 改写 | 固定 upstream + 原始 query 测试；不手工拼 URL |
| HEAD 403 | HEAD 留后端，不走 GET presign |
| 响应头重复/丢失 | `proxy_hide_header` + internal location 重建；测试 |
| missing_at 漂移 | 本轮只做预校验；预校验后丢失不更新，写入已知边界 |
| 路径穿越 | 后端 key 约束（无 `//`/`.`/`..`）+ 双侧编码规范化负向测试；不依赖 nginx 全局 `merge_slashes` |
| 并发上限不等价 | 标准 nginx 只近似；严格 8 流需 OpenResty/njs 或保留后端流式 |
| endpoint 漂移 | nginx 的 `minio:9000`/Host 与 `settings.STORAGE_ENDPOINT` 必须由同一部署模板渲染，并做一致性验收 |
| 真实 IP 失真 | SLB 后必须覆盖/规范化 XFF 并配置可信 `set_real_ip_from`；否则 per-IP 限流可被伪造或误伤全体 |
| 自动回退假承诺 | 明确 502/504；回退需独立 error_page 设计 |

待确认：

- 是否允许新增 edge-nginx/修改 compose（需单独批准）；
- 是否能关闭后端宿主端口暴露，SSE 是否稳定经 edge/Next；
- MinIO 内部 presign 是否可接受在 header 中短暂出现；
- 若不能接受，是否改用 opaque token + sidecar/njs，代价是新增组件。

## 14. 完成标准

只有满足以下条件，才能把 X-Accel 标为“已验收”：

- `/code/` 与 saved-media 均命中 edge，不经 Next 透传；
- backend/frontend 端口网络隔离，直连伪造测试通过；
- X1–X20 及 X4c/X12b 全部有可复现日志/命令/镜像版本；
- Range/HEAD/416、下载、缓存头、内部 location、SSE 长连均有证据；
- nginx `limit_conn`/`limit_req` 与后端流式回退指标均可见；
- missing_at 漂移作为已知边界被记录；
- `enabled=false` 回滚路径正常；
- 真实并发、慢客户端、MinIO 失败/断线已实测。

在此之前，只能写“X-Accel 方案提案/实验”，不能写“R15 已解决”。
