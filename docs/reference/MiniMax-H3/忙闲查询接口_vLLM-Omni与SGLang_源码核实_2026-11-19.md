# vLLM-Omni 与 SGLang 忙闲查询接口（源码核实）

> 核实时间：2026-11-19
> 核实方式：**直接读本地源码**，不采信网文/二手盘点
> 源码路径：
> - `\\wsl.localhost\Ubuntu\home\wmd\projects\vllm-omni`
> - `\\wsl.localhost\Ubuntu\home\wmd\projects\sglang`
> 版本说明：两个仓库均**无 `.git` 目录**，版本号由 setuptools-scm 动态生成，**无法从快照确定精确版本**。下述结论对应当前快照；升级后必须重新 grep 核对指标名。

---

## 0. 结论速查

| 框架 | 接口 | 路径 | 忙闲判据 | 前置条件 |
|---|---|---|---|---|
| vLLM-Omni | 作业列表（推荐） | `GET /v1/videos` | `data[].status ∈ {queued, in_progress}` 数量 | 异步作业模式；**单 API worker** |
| vLLM-Omni | 单作业查询 | `GET /v1/videos/{id}` | `status` 字段 | 同上 |
| vLLM-Omni | 指标 | `GET /metrics` | `vllm_omni:num_requests_running` / `..._waiting` | 服务内建 |
| SGLang（LLM 引擎） | 指标 | `GET /metrics` | `sglang:num_running_reqs` / `sglang:num_queue_reqs` | 启动加 `--enable-metrics` |
| SGLang（**diffusion/视频**） | 指标 | `GET /metrics` | `sglang:diffusion_num_running_reqs` / `..._num_queue_reqs` | 启动加 `--enable-metrics`，否则 404 |

**选哪个**：H3 视频生成走的是 diffusion 通道 → 优先看 `GET /v1/videos` 的 status（最贴近"任务"语义），`sglang:diffusion_*` 作补充。LLM 场景才用 `sglang:num_running_reqs`。

---

## 1. vLLM-Omni

### 1.1 作业状态接口（任务级，推荐）

源码位置：
- 路由：`vllm_omni/entrypoints/openai/api_server.py:3311` `list_videos()`
- 数据模型：`vllm_omni/entrypoints/openai/protocol/videos.py:29-34, 468-547`

**端点**

```
GET /v1/videos?after=<cursor>&limit=<0-100>&order=<asc|desc>
```
- `order` 默认 `desc`（按 `created_at` 倒序）
- `limit` 上限 100
- 返回 `VideoListResponse`：

```json
{
  "object": "list",
  "first_id": "video_gen_xxx",
  "last_id": "video_gen_yyy",
  "has_more": false,
  "data": [
    {
      "id": "video_gen_xxx",
      "object": "video",
      "model": "...",
      "prompt": "...",
      "status": "in_progress",
      "progress": 45,
      "created_at": 1760000000,
      "completed_at": null,
      "inference_time_s": null,
      "error": null,
      ...
    }
  ]
}
```

**状态枚举（源码确认，仅 4 个值）** — `VideoGenerationStatus`：

| 值 | 含义 |
|---|---|
| `queued` | 已建作业，未开始 |
| `in_progress` | 正在生成 |
| `completed` | 完成 |
| `failed` | 失败（此时 `GET /v1/videos/{id}` 返回非 200，错误码由 `_status_code_for_video_failure` 决定） |

- `progress` 字段：`int`，0~100，源码注明是 "best-effort progress indicator"，**只能看趋势，不能当精确进度**。
- 单个作业：`GET /v1/videos/{id}`，不存在返回 404。

> ⚠️ **纠正上一轮口头回答**：我说过状态含 `cancelled` —— **源码里没有**。只有上述 4 个。`realtime_events.py` 里的 `in_progress` 属于 realtime 协议，与视频作业状态机无关。

> ⚠️ **服务化红线**：`_require_single_api_video_store`（api_server.py:1766）明确：
> `VIDEO_STORE` / `VIDEO_TASKS` 是 **process-local**，多 API worker 时**所有作业端点直接被拒绝**（`_reject_process_local_state_with_multiple_api_workers`）。
> 即：**不能用多副本 API 分担视频作业**，否则 `GET /v1/videos` 看不到别的 worker 的作业。这是当前快照的硬约束。

### 1.2 指标接口（引擎级）

源码位置：
- `vllm_omni/metrics/definitions.py:26, 180` — `METRIC_PREFIX = "vllm_omni:"`
- `vllm_omni/metrics/prometheus.py:12-21` — Gauge 定义
- `vllm_omni/entrypoints/omni_base.py:425-443` — `_publish_request_gauges()`

| 指标名 | 类型 | 源码原义 |
|---|---|---|
| `vllm_omni:num_requests_running` | Gauge | "Number of requests currently running across all pipeline stages." |
| `vllm_omni:num_requests_waiting` | Gauge | "Number of requests waiting to be scheduled." |

发布逻辑（omni_base.py:425-443）：
```
dispatched        = engine._running_counter        # 已派发到 stage engine（含其内部排队）
in_engine_waiting = engine._engines_waiting_counter # stage scheduler 内还在等的
running = dispatched - in_engine_waiting
waiting = (total - dispatched) + in_engine_waiting
```
注意命名空间是 **`vllm_omni:`**，不是上游 vLLM 的 `vllm:`。上游 `vllm:num_requests_running` 是否存在取决于打包的 vLLM 本体，本仓库无法确认——实测用 `curl -s <host>/metrics | grep -E 'num_requests_(running|waiting)'`。

### 1.3 探针代码（vLLM-Omni）

```python
import os
import requests

BASE = "http://<server>:8091"  # 改成实际地址

def omni_busy(base: str = BASE, timeout: float = 10.0) -> tuple[bool, list[dict]]:
    """返回 (是否忙, 忙的作业列表)。"""
    r = requests.get(f"{base}/v1/videos", params={"limit": 100, "order": "desc"},
                     timeout=timeout)
    r.raise_for_status()
    jobs = r.json()["data"]                      # 注意：不是裸 list，有 data 包装
    busy = [j for j in jobs
            if j.get("status") in ("queued", "in_progress")]
    return bool(busy), busy

def omni_metrics_busy(base: str = BASE) -> int:
    """返回 running gauge 的整数值。"""
    text = requests.get(f"{base}/metrics", timeout=10).text
    for line in text.splitlines():
        if line.startswith("vllm_omni:num_requests_running "):
            return int(float(line.rsplit(" ", 1)[1]))
    return 0

if __name__ == "__main__":
    busy, jobs = omni_busy()
    print("忙" if busy else "空闲", [(j["id"], j["status"], j.get("progress")) for j in jobs])
    print("running =", omni_metrics_busy())
```

---

## 2. SGLang

SGLang 有**两套**指标，取决于跑的是 LLM 还是 diffusion（视频/图像生成）。H3 属于后者。

### 2.1 LLM 引擎指标

源码位置：`python/sglang/srt/observability/metrics_collector.py:377-388`

| 指标名 | 类型 | 源码原义 |
|---|---|---|
| `sglang:num_running_reqs` | Gauge | "The number of running requests." |
| `sglang:num_queue_reqs` | Gauge | "The number of requests in the waiting queue." |
| `sglang:num_grammar_queue_reqs` | Gauge | grammar 等待队列 |
| `sglang:token_usage` | Gauge | KV token 池占用率 |

### 2.2 diffusion / 视频生成指标（H3 用这套）

源码位置：`python/sglang/multimodal_gen/runtime/observability/metrics.py:70-103`

| 指标名 | 类型 | 源码原义 | label |
|---|---|---|---|
| `sglang:diffusion_num_queue_reqs` | Gauge | "Requests waiting for first dispatch." | `role`, `replica` |
| `sglang:diffusion_num_running_reqs` | Gauge | "Dispatched requests not yet completed." | `role`, `replica` |
| `sglang:diffusion_requests_total` | Counter | 完成的原始请求数 | `role`,`replica`,`status`,`is_warmup` |
| `sglang:diffusion_request_latency_seconds` | Histogram | 调度接受到完成的耗时 | 同上 |

计数语义（源码 `enqueue` / `dispatch` / `finish`）：
- 作业进入 → `queue += 1`
- 首次派发 → `queue -= 1`, `running += 1`
- 完成/失败 → 对应减 1

**启用条件**（关键）：
- `init_metrics()`：`if not server_args.enable_metrics or rank % group_size: return None`
  → 必须 `--enable-metrics`；且**只有每个 replica 的 leader rank 发布**，多卡时按 `replica` label 过滤，否则会重复计数。
- `/metrics` 路由：`python/sglang/multimodal_gen/runtime/entrypoints/http_server.py:87`
- 单元测试 `test_diffusion_metrics.py:286-287` 明确断言：`enable_metrics=False` 时 `GET /metrics` 返回 **404**。

### 2.3 探针代码（SGLang diffusion）

```python
import re
import requests

BASE = "http://<server>:30000"

def sglang_diffusion_busy(base: str = BASE, timeout: float = 10.0) -> dict:
    r = requests.get(f"{base}/metrics", timeout=timeout)
    if r.status_code == 404:
        raise RuntimeError("未加 --enable-metrics，/metrics 返回 404")
    r.raise_for_status()
    out = {"running": 0, "queue": 0, "by_replica": {}}
    # 格式：sglang:diffusion_num_running_reqs{role="monolithic",replica="0"} 1.0
    pat = re.compile(r'^(sglang:diffusion_num_(?:running|queue)_reqs)\{([^}]*)\}\s+([\d.]+)$')
    for line in r.text.splitlines():
        m = pat.match(line)
        if not m:
            continue
        name, labels, val = m.group(1), m.group(2), float(m.group(3))
        kv = dict(re.findall(r'(\w+)="([^"]*)"', labels))
        rep = kv.get("replica", "?")
        key = "running" if "running" in name else "queue"
        out[key] += val
        out["by_replica"].setdefault(rep, {})[key] = val
    return out

def sglang_llm_busy(base: str = BASE) -> tuple[float, float]:
    """LLM 场景：返回 (running, queue)。"""
    text = requests.get(f"{base}/metrics", timeout=10).text
    def first(prefix):
        for line in text.splitlines():
            if line.startswith(prefix + "{"):
                return float(line.rsplit(" ", 1)[1])
        return 0.0
    return first("sglang:num_running_reqs"), first("sglang:num_queue_reqs")
```

---

## 3. 引子：为什么"先查再发"仍是软闸门

两个绕不开的问题：

1. **TOCTOU 竞态**：A 查到空闲 → B 也查到空闲 → 两个同时提交。视频作业 820s，竞态窗口极大。
2. **快照滞后**：`/metrics` 是采样时刻值，`GET /v1/videos` 的 status 写库也有延迟。

`--enable-metrics` / 查 status 只能用于**观测和告警**。要真正保证"单卡同时只有一个任务"，必须在**网关层做单飞锁**（Redis `SETNX` 或文件锁），把锁的生命周期覆盖「提交 → 作业终态」整个区间，引擎指标只作第二道防线。

```python
import json
import time
import uuid
import requests
import redis

rds = redis.Redis(host="<redis>", decode_responses=True)
BASE = "http://<server>:8091"
LOCK = "h3:video:singleflight"
TERMINAL = {"completed", "failed"}

def submit_exclusive(form: dict, ttl: int = 7200) -> str:
    token = uuid.uuid4().hex
    if not rds.set(LOCK, token, nx=True, ex=ttl):
        raise RuntimeError("已有任务在跑，拒绝提交")
    try:
        vid = requests.post(f"{BASE}/v1/videos", data=form, timeout=60).json()["id"]
        while True:
            job = requests.get(f"{BASE}/v1/videos/{vid}", timeout=30).json()
            if job.get("status") in TERMINAL:
                return vid
            time.sleep(5)
    finally:
        # Lua 保证只删自己的锁，避免误删他人续期后的锁
        rds.eval(
            "if redis.call('get',KEYS[1])==ARGV[1] then "
            "return redis.call('del',KEYS[1]) else return 0 end",
            1, LOCK, token,
        )
```

> 若服务异常退出导致锁残留，靠 `ex=ttl` 自动过期兜底；ttl 必须大于单次最长作业时间。

---

## 4. 落地建议（单卡 40GB 场景）

1. **先启动层限流**：vLLM `--max-num-seqs 1` / SGLang `--max-running-requests 1`（vLLM-Omni diffusion 是否吃这个参数要在 H3 上实测），让引擎自己排队。
2. **网关层单飞锁**：如上，杜绝跨客户端竞态。
3. **观测层**：Grafana 盯 `vllm_omni:num_requests_running` / `sglang:diffusion_num_running_reqs`，**只告警，不做准入**。
4. **多副本注意**：vLLM-Omni 异步作业 store 是进程本地的，多 API worker 会让作业端点失效——当前阶段保持单 API worker。

---

## 5. 源码索引（便于复核）

| 事实 | 文件:行 |
|---|---|
| vLLM-Omni 状态枚举 4 值 | `vllm_omni/entrypoints/openai/protocol/videos.py:29-34` |
| `VideoResponse` 字段（含 progress） | 同上 `:468-525` |
| `VideoListResponse` 包装结构 | 同上 `:540-547` |
| `GET /v1/videos` 路由与参数 | `vllm_omni/entrypoints/openai/api_server.py:3311-3345` |
| process-local store 限制 | 同上 `:1766-1770` |
| `vllm_omni:` 命名空间 | `vllm_omni/metrics/definitions.py:26,180` |
| running/waiting Gauge 定义 | `vllm_omni/metrics/prometheus.py:12-21` |
| running/waiting 计算逻辑 | `vllm_omni/entrypoints/omni_base.py:425-443` |
| SGLang LLM running/queue 指标 | `python/sglang/srt/observability/metrics_collector.py:377-388` |
| SGLang diffusion 指标定义 | `python/sglang/multimodal_gen/runtime/observability/metrics.py:70-103` |
| diffusion 需 enable_metrics / 只 leader 发布 | 同上 `:28-41` |
| `/metrics` 未启用返回 404 | `python/sglang/multimodal_gen/test/unit/test_diffusion_metrics.py:286-287` |
