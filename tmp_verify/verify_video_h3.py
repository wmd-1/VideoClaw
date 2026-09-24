"""验证 MiniMax-H3 双协议适配：vllm-omni（extra_params/input_references）与 sglang（task）。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_video_h3.py
"""
import io
import json
import os
import sys

sys.path.insert(0, "/app")
# 测试辅助：通过 VC_PATCHED_BACKEND 优先加载补丁代码副本（镜像重建后无需该变量）
if os.environ.get("VC_PATCHED_BACKEND"):
    sys.path.insert(0, os.environ["VC_PATCHED_BACKEND"])
from models.custom_video import CustomVideoClient  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


p1, p2 = "/tmp/h3_a.png", "/tmp/h3_b.png"
for path in (p1, p2):
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 16)


def make_client(protocol):
    return CustomVideoClient(
        {"id": "h3", "request_model": "MiniMax-H3", "base_url": "http://127.0.0.1:1/v1", "protocol": protocol, "api_key": ""}
    )


def variant_summary(variants):
    out = []
    for encoding, files, form in variants:
        fields = [name for name, _ in files]
        out.append((encoding, fields, form.get("task") or form.get("extra_params", "")))
    return out


# ── vllm-omni ──
vm = make_client("vllm-omni")

v = vm._create_variants([[]], vm._task_for_input(None, None, []), 5)
enc, files, form = v[0]
check("vllm-omni 文生视频：extra_params.task=t2va", enc == "multipart" and json.loads(form["extra_params"])["task"] == "t2va", str(variant_summary(v)[:2]))
check("vllm-omni 文生视频：保留旧形态回退", any(not f for _, _, f in v), str([bool(f) for _, _, f in v]))

v = vm._create_variants(vm._image_file_specs(p1, None, None), "fl2va", 5)
enc, files, form = v[0]
check("vllm-omni 首帧：input_reference + task=fl2va", [n for n, _ in files] == ["input_reference"] and json.loads(form["extra_params"])["task"] == "fl2va", str(variant_summary(v)[:2]))

v = vm._create_variants(vm._image_file_specs(p1, p2, None), "fl2va", 5)
enc, files, form = v[0]
params = json.loads(form["extra_params"])
check(
    "vllm-omni 首尾帧：input_references×2 + frame_indices=[0,-1]",
    [n for n, _ in files] == ["input_references", "input_references"] and params.get("frame_indices") == [0, -1],
    str(variant_summary(v)[:2]),
)

v = vm._create_variants(vm._image_file_specs(None, None, [p1, p2]), "ref2va", 5)
enc, files, form = v[0]
ref_params = json.loads(form["extra_params"])
check(
    "vllm-omni 多参考图：input_references×2 + task=ref2va（无 frame_indices）",
    [n for n, _ in files] == ["input_references", "input_references"]
    and ref_params["task"] == "ref2va"
    and "frame_indices" not in ref_params,
    str(variant_summary(v)[:2]),
)

# ── sglang ──
sg = make_client("sglang")

v = sg._create_variants([[]], "t2va", 5)
enc, files, form = v[0]
check("sglang 文生视频：JSON + task=t2va", enc == "json" and form.get("task") == "t2va", str(variant_summary(v)))

v = sg._create_variants(sg._image_file_specs(p1, None, None), "fl2va", 5)
enc, files, form = v[0]
check("sglang 首帧：multipart + task=fl2va + input_reference", enc == "multipart" and form.get("task") == "fl2va" and [n for n, _ in files] == ["input_reference"], str(variant_summary(v)[:2]))

v = sg._create_variants(sg._image_file_specs(p1, p2, None), "fl2va", 5)
enc, files, form = v[0]
check("sglang 首尾帧：顺序双 input_reference + task=fl2va", [n for n, _ in files] == ["input_reference", "input_reference"] and form.get("task") == "fl2va", str(variant_summary(v)[:2]))

v = sg._create_variants(sg._image_file_specs(None, None, [p1, p2]), "ref2va", 5)
enc, files, form = v[0]
check("sglang 多参考图：input_reference×2 + task=ref2va", [n for n, _ in files] == ["input_reference", "input_reference"] and form.get("task") == "ref2va", str(variant_summary(v)[:2]))

# ── 仅尾帧与 sglang conditions（官方 cookbook 形态） ──
v = vm._create_variants(vm._image_file_specs(None, p2, None), "fl2va", 5, tail_only=True)
enc, files, form = v[0]
check(
    "vllm-omni 仅尾帧：input_reference + frame_indices=[-1]",
    [n for n, _ in files] == ["input_reference"] and json.loads(form["extra_params"]).get("frame_indices") == [-1],
    str(variant_summary(v)[:2]),
)

cond_variant = sg._sglang_conditions_variant(p1, p2, [], "fl2va")
enc, files, form = cond_variant
check(
    "sglang conditions：首尾帧 frame_index 0/-1（keyframe）",
    enc == "json"
    and form["task"] == "fl2va"
    and [c.get("frame_index") for c in form["conditions"]] == [0, -1]
    and all(c.get("role") == "keyframe" for c in form["conditions"]),
    str(form),
)
cond_refs = sg._sglang_conditions_variant(None, None, [p1, p2], "ref2va")
check(
    "sglang conditions：参考图 role=reference",
    all(c.get("role") == "reference" for c in cond_refs[2]["conditions"]) and len(cond_refs[2]["conditions"]) == 2,
    str(cond_refs[2]),
)
v = sg._create_variants([[]], "t2va", 5)
check("sglang 文生：JSON 含 conditions=[]", v[0][2].get("conditions") == [], str(v[0][2]))

# ── openai：保持原形态（无 task/extra_params） ──
op = make_client("openai")

v = op._create_variants(op._image_file_specs(p1, p2, None), "fl2va", 5)
check("openai 首尾帧：保持原字段候选且无附加字段", all(not f for _, _, f in v), str(variant_summary(v)[:2]))
v = op._create_variants([[]], "t2va", 5)
check("openai 文生视频：无 task/extra_params", all(not f for _, _, f in v), str(variant_summary(v)))

# ── _post_create：表单组装（extra_params 进入 data；同名多文件保留） ──
captured = {}


class StubResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"id": "job-1", "status": "queued"}


def fake_post(path, **kwargs):
    captured["path"] = path
    captured["kwargs"] = kwargs
    return StubResponse()


vm._client.post = fake_post
vm._post_create(
    "multipart", "prompt-x", [("input_references", p1), ("input_references", p2)], "1280x720", 5, None, None,
    {"extra_params": json.dumps({"task": "fl2va", "duration": 5.0, "frame_indices": [0, -1]})},
)
files = captured["kwargs"].get("files")
data = captured["kwargs"].get("data", {})
check("multipart 同名多文件保留", isinstance(files, list) and [f[0] for f in files] == ["input_references", "input_references"], str(files and [f[0] for f in files]))
check("multipart data 含 extra_params JSON 字符串", "extra_params" in data and json.loads(data["extra_params"])["task"] == "fl2va", str(data.get("extra_params"))[:80])

# ── 媒体参考：audio_reference 字段与视频参考合并（vllm-omni ref2va） ──
v1, v2 = "/tmp/h3_v1.mp4", "/tmp/h3_v2.mp4"
for path in (v1, v2):
    with open(path, "wb") as f:
        f.write(b"\x00\x00\x00\x18ftypmp42" + b"v" * 16)

audio_url = "http://127.0.0.1:9999/audio/ref.wav"
check("audio_reference 字段：JSON 字符串含 audio_url", json.loads(vm._audio_reference_field(audio_url)) == {"audio_url": audio_url}, str(vm._audio_reference_field(audio_url)))
check("audio_reference 缺省：不注入字段", vm._audio_reference_field(None) is None and vm._audio_reference_field("") is None)
check("sglang 不支持音频参考：显式忽略（不静默）", sg._audio_reference_field(audio_url) is None and any("audio_reference" in a for a in sg._adjustments), str(sg._adjustments))

merged = vm._image_file_specs(None, None, [p1], [v1])
check("图片+视频合并：input_references 按序（图在前）", merged[0] == [("input_references", p1), ("input_references", v1)], str(merged[0]))
check("单视频参考：input_reference 单文件", vm._image_file_specs(None, None, [], [v1])[0] == [("input_reference", v1)], str(vm._image_file_specs(None, None, [], [v1])[0]))
mixed = vm._image_file_specs(None, None, [p1, p2], [v1, v2])
check("双图+双视频：input_references ×4 按序", [n for n, _ in mixed[0]] == ["input_references"] * 4 and [p for _, p in mixed[0]] == [p1, p2, v1, v2], str(mixed[0]))
check("纯图片参考行为不变：input_references ×2", vm._image_file_specs(None, None, [p1, p2])[0] == [("input_references", p1), ("input_references", p2)])

# 音频参考进入 sync 表单（双链路一致性：_post_sync 与 _post_create 共用 audio_reference 入参）
vm2 = make_client("vllm-omni")
vm2._client.post = fake_post
audio_field = json.dumps({"audio_url": audio_url})
vm2._post_sync("p", [("input_reference", p1)], "1280x720", 5, None, None, {"extra_params": "{}"}, audio_reference=audio_field)
check("sync 表单含 audio_reference JSON", captured["kwargs"]["data"]["audio_reference"] == audio_field, str(captured["kwargs"]["data"].get("audio_reference"))[:80])
vm2._post_create("multipart", "p", [("input_references", v1), ("input_references", v2)], "1280x720", 5, None, None, audio_reference=audio_field)
files2 = captured["kwargs"]["files"]
check("async 表单含 audio_reference JSON", captured["kwargs"]["data"]["audio_reference"] == audio_field, str(captured["kwargs"]["data"].get("audio_reference"))[:80])
check("async 视频参考文件字段保留", [f[0] for f in files2] == ["input_references", "input_references"], str([f[0] for f in files2]))

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)