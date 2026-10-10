"""MiniMax-H3 任务类型与推理服务 partition 的收敛验证（capabilities.tasks）。

背景：vllm-omni 内存受限 profile 用 --task-type fl2va|ref2va 只加载单个 DiT partition，
task 与 partition 不符会被服务端 500（checkpoint partition 'ref2va' supports ['ref2va'], got task='fl2va'）。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_task_partitions.py
"""
import json
import os
import sys

sys.path.insert(0, "/app")
from models.custom_video import CustomVideoClient  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


for path in ("/tmp/tp_first.png", "/tmp/tp_last.png", "/tmp/tp_ref.png"):
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)


def client(protocol="vllm-omni", capabilities=None):
    meta = {"id": "h3", "request_model": "/models/MiniMax-H3", "base_url": "http://127.0.0.1:1/v1", "protocol": protocol, "api_key": ""}
    if capabilities is not None:
        meta["capabilities"] = capabilities
    return CustomVideoClient(meta)


def align(c, image=None, last=None, refs=None, videos=None):
    return c._align_task_to_partitions(image, last, refs, videos)


# 1) 未声明 tasks：零改动（向后兼容）
plain = client()
check("未声明 tasks 时首帧仍推断为 fl2va", plain._task_for_input("/tmp/tp_first.png", None, []) == "fl2va")
img, last, refs = align(plain, image="/tmp/tp_first.png", last="/tmp/tp_last.png")
check("未声明 tasks 时输入布局不变", (img, last, refs) == ("/tmp/tp_first.png", "/tmp/tp_last.png", None), str([img, last, refs]))

# 2) capabilities.tasks 解析（列表 / 逗号串 / 非法项过滤）
check("tasks 列表解析", client(capabilities={"tasks": ["ref2va", "FL2VA"]})._supported_tasks() == ["ref2va", "fl2va"])
check("tasks 逗号串解析", client(capabilities={"tasks": "ref2va, t2va"})._supported_tasks() == ["ref2va", "t2va"])
check("tasks 非法项被过滤", client(capabilities={"tasks": ["ref2va", "bogus"]})._supported_tasks() == ["ref2va"])

# 3) 只挂 ref2va partition：首帧请求降级为参考图
only_ref = client(capabilities={"tasks": ["ref2va"]})
img, last, refs = align(only_ref, image="/tmp/tp_first.png", last="/tmp/tp_last.png")
check("首尾帧在 ref2va-only 实例下降级为参考图", (img, last) == (None, None) and refs == ["/tmp/tp_first.png", "/tmp/tp_last.png"], str([img, last, refs]))
check("降级事实被记录", any("ref2va" in item and "partition" in item for item in only_ref._adjustments), str(only_ref._adjustments))
check("降级后 task 为 ref2va", only_ref._task_for_input(img, last, only_ref._media_reference_paths(refs, None)) == "ref2va")

# 4) 任务本就是 ref2va 时，首/尾帧图也并入参考图（避免首帧字段与参考字段混用）
mixed = client(capabilities={"tasks": ["ref2va"]})
_img, _last, _refs = mixed._align_task_to_partitions("/tmp/tp_first.png", None, ["/tmp/tp_ref.png"], None)
check("首帧图排在既有参考图之前", (_img, _last) == (None, None) and _refs == ["/tmp/tp_first.png", "/tmp/tp_ref.png"], str([_img, _last, _refs]))

# 5) 含图但实例不支持任何含图任务 → 明确失败，不静默丢图
only_text = client(capabilities={"tasks": ["t2va"]})
try:
    only_text._align_task_to_partitions("/tmp/tp_first.png", None, None, None)
    check("含图遇 t2va-only 实例明确失败", False, "未抛错")
except ValueError as exc:
    check("含图遇 t2va-only 实例明确失败", "不匹配" in str(exc) and "不会静默去掉图片" in str(exc), str(exc)[:110])

# 6) 声明支持 fl2va 时不触发降级
full = client(capabilities={"tasks": ["t2va", "fl2va", "ref2va"]})
img, last, refs = full._align_task_to_partitions("/tmp/tp_first.png", None, None, None)
check("支持 fl2va 时首帧保持首帧语义", (img, last, refs) == ("/tmp/tp_first.png", None, None), str([img, last, refs]))

# 7) openai 协议不受影响（无 partition 概念）
oa_img, oa_last, oa_refs = client("openai", capabilities={"tasks": ["ref2va"]})._align_task_to_partitions("/tmp/tp_first.png", None, None, None)
check("未声明协议差异：openai 同样按声明收敛", (oa_img, oa_last) == (None, None) and oa_refs == ["/tmp/tp_first.png"], str([oa_img, oa_last, oa_refs]))

# 8) 端到端字段：降级后 multipart 用 input_references、extra_params.task=ref2va 且不带 frame_indices
captured = []


class StubResponse:
    status_code = 200
    text = "{}"
    headers = {"content-type": "video/mp4"}
    content = b"\x00\x00\x00\x18ftypmp42"

    def json(self):
        return {"id": "job-1", "status": "queued"}


def fake_post(path, **kwargs):
    captured.append({"path": path, **kwargs})
    return StubResponse()


e2e = client(capabilities={"tasks": ["ref2va"], "short_edge": 768})
e2e._client.post = fake_post
e2e.generate_video(prompt="p", image_path="/tmp/tp_first.png", save_path="/tmp/tp_out.mp4", duration=5, video_ratio="16:9", resolution="768P")
body = captured[0]
data = dict(body.get("data") or {})
for key, value in (body.get("files") or {}).items() if isinstance(body.get("files"), dict) else []:
    data[key] = value[1] if isinstance(value, tuple) else value
for key, value in (body.get("files") or []) if isinstance(body.get("files"), list) else []:
    data[key] = value[1] if isinstance(value, tuple) else value
check(
    "E2E 走同步端点且图片按参考字段下发",
    body["path"].endswith("/videos/sync") and "input_reference" in str(data),
    str(sorted(data)),
)
check("E2E extra_params.task 为 ref2va", json.loads(data.get("extra_params", "{}")).get("task") == "ref2va", str(data.get("extra_params")))
check("E2E 降级后不携带 frame_indices", "frame_indices" not in json.loads(data.get("extra_params", "{}")), str(data.get("extra_params")))
check("E2E 短边仍按声明 768 下发", data.get("short_edge") == "768", str(data.get("short_edge")))

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)