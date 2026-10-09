"""任务 9.1/9.2/9.3 验证：capabilities 采样调度参数的解析、按协议注入与零变化护栏。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_sampling_params.py
"""
import io
import json
import logging
import sys

sys.path.insert(0, "/app")
import httpx  # noqa: E402
from models.custom_video import CustomVideoClient  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


SCHED_KEYS = {"num_inference_steps", "flow_shift", "audio_flow_shift"}


def client(protocol, capabilities=None):
    meta = {"id": "m", "request_model": "MiniMax-H3", "base_url": "http://127.0.0.1:1/v1", "protocol": protocol, "api_key": ""}
    if capabilities is not None:
        meta["capabilities"] = capabilities
    return CustomVideoClient(meta)


def mapped(c):
    return c._map_protocol_params(duration=5, video_ratio="16:9", resolution="768P", fps=None, short_edge=None)


# ── 9.3 未声明：零变化护栏 ──
for proto in ("vllm-omni", "sglang", "openai"):
    p, j, _drop = mapped(client(proto, {"duration": {"min": 4, "max": 15}}))
    leaked = (set(p) | set(j)) & SCHED_KEYS
    check(f"9.3 {proto} 未声明调度参数时零注入", not leaked, str(sorted(leaked)))
    check(f"9.3 {proto} extra_params 不含 audio_flow_shift", "audio_flow_shift" not in client(proto)._extra_params("t2va", 5), "")

# ── 9.1 非法/非正值忽略并记录 ──
logs = io.StringIO()
handler = logging.StreamHandler(logs)
logging.getLogger("models.custom_video").addHandler(handler)
logging.getLogger("models.custom_video").setLevel(logging.WARNING)
p_bad, j_bad, _ = mapped(client("sglang", {"num_inference_steps": 0, "flow_shift": "abc", "audio_flow_shift": -1}))
logging.getLogger("models.custom_video").removeHandler(handler)
check("9.1 非法值全部忽略（不注入也不报错）", not ((set(p_bad) | set(j_bad)) & SCHED_KEYS), str([p_bad, j_bad]))
check("9.1 忽略事实被记录（≥3 条）", logs.getvalue().count("ignored") >= 3, logs.getvalue().replace("\n", " ")[:160])

# ── 9.2 vllm-omni：顶层 num_inference_steps/flow_shift + extra_params.audio_flow_shift ──
vllm = client("vllm-omni", {"short_edge": 768, "num_inference_steps": 5, "flow_shift": 12, "audio_flow_shift": 3})
p, j, _drop = mapped(vllm)
check("9.2 vllm-omni 顶层 steps/flow_shift", p.get("num_inference_steps") == "5" and p.get("flow_shift") == "12", str(p))
check("9.2 vllm-omni flow_shift 整数值不带小数点", p.get("flow_shift") == "12", str(p.get("flow_shift")))
check("9.2 vllm-omni audio_flow_shift 归入 extra_params", json.loads(vllm._extra_params("t2va", 5)).get("audio_flow_shift") == 3.0, vllm._extra_params("t2va", 5))
check("9.2 vllm-omni 顶层不重复下发 audio_flow_shift", "audio_flow_shift" not in p, str(p))

# ── 9.2 sglang：三者均为 JSON 顶层 ──
sg = client("sglang", {"short_edge": 768, "num_inference_steps": 5, "flow_shift": 6, "audio_flow_shift": 3})
p, j, _ = mapped(sg)
check(
    "9.2 sglang JSON 顶层三字段",
    j.get("num_inference_steps") == 5 and j.get("flow_shift") == 6 and j.get("audio_flow_shift") == 3,
    str(j),
)
check("9.2 sglang target 与调度字段共存", isinstance(j.get("target"), dict) and j["target"]["short_edge"] == 768, str(j.get("target")))

# ── 9.2 FastH3 互斥 ──
fast = client("sglang", {"short_edge": 768, "num_inference_steps": 4, "flow_shift": 12, "audio_flow_shift": 3, "fast_h3": True})
p, j, _ = mapped(fast)
check("9.2 FastH3 仅下发 steps=4", j.get("num_inference_steps") == 4, str(j))
check("9.2 FastH3 丢弃两个 shift", not ({"flow_shift", "audio_flow_shift"} & set(j)), str(j))
check("9.2 FastH3 extra_params 也不带 audio_flow_shift", "audio_flow_shift" not in json.loads(fast._extra_params("t2va", 4)), fast._extra_params("t2va", 4))

# ── 真实编码链路（multipart/JSON 实际字段） ──
captured = []


class StubResponse:
    status_code = 200
    text = "{}"
    headers = {"content-type": "application/json"}
    content = b"{}"

    def json(self):
        return {"id": "job-1", "status": "queued"}


def fake_post(path, **kwargs):
    captured.append({"path": path, **kwargs})
    return StubResponse()


class StubVideoResponse(StubResponse):
    headers = {"content-type": "video/mp4"}
    content = b"\x00\x00\x00\x18ftypmp42"


def fake_post_sync(path, **kwargs):
    captured.append({"path": path, **kwargs})
    return StubVideoResponse()


def form_fields(body):
    """合并 multipart 表单字段：无文件时 httpx 会把标量放进入 files（filename=None），有文件时在 data。"""
    fields = dict(body.get("data") or {})
    files = body.get("files")
    items = files.items() if isinstance(files, dict) else (files or [])
    for key, value in items:
        fields[key] = value[1] if isinstance(value, tuple) else value
    return fields


# 异步创建链路（真实入口 generate_video 会把映射结果传下来，此处等价传参）
for proto in ("vllm-omni", "sglang"):
    c = client(proto, {"short_edge": 768, "num_inference_steps": 5, "flow_shift": 12, "audio_flow_shift": 3})
    p, j, drop = mapped(c)
    c._client.post = fake_post
    c._create_job(
        prompt="p", image_path=None, last_image_path=None, reference_image_paths=None,
        size="1344x768", duration=5, negative_prompt=None, seed=None,
        protocol_fields=p, json_fields=j, drop_size=drop,
    )
    body = captured[-1]
    if proto == "sglang":
        sent = body.get("json") or {}
        check("async sglang JSON 实际含三调度字段", {"num_inference_steps", "flow_shift", "audio_flow_shift"} <= set(sent), str(sorted(sent)))
        check("async sglang 同时含 target", isinstance(sent.get("target"), dict), str(sent.get("target")))
    else:
        data = form_fields(body)
        check("async vllm-omni 表单含 num_inference_steps/flow_shift", "num_inference_steps" in data and "flow_shift" in data, str(sorted(data)))
        check("async vllm-omni 声明 short_edge 时不再下发冲突 size", "size" not in data and data.get("short_edge") == "768", str(sorted(data)))
        check("async vllm-omni extra_params 携 audio_flow_shift", "audio_flow_shift" in str(data.get("extra_params", "")), str(data.get("extra_params"))[:120])

# 同步链路一致性（D5：不得只在异步链路注入）
sync_client = client("vllm-omni", {"short_edge": 768, "num_inference_steps": 5, "flow_shift": 12, "audio_flow_shift": 3})
sync_client._client.post = fake_post_sync
sync_client.generate_video(prompt="p", image_path=None, save_path="/tmp/sched_sync.mp4", duration=5)
sync_body = captured[-1]
sync_data = form_fields(sync_body)
check("sync 端点路径", sync_body["path"].endswith("/videos/sync"), str(sync_body["path"]))
check("sync 与 async 一致地含 num_inference_steps/flow_shift", "num_inference_steps" in sync_data and "flow_shift" in sync_data, str(sorted(sync_data)))
check("sync 落盘成功", open("/tmp/sched_sync.mp4", "rb").read(4) == b"\x00\x00\x00\x18", "")

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)