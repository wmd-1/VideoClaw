"""验证自定义视频多图支持：首尾帧/参考图字段候选、编码组合与前端三能力可查询。"""
import json
import os
import sys
import urllib.request

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


p1, p2 = "/tmp/vc_a.png", "/tmp/vc_b.png"
for path in (p1, p2):
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 16)


def make_client(protocol="vllm-omni"):
    return CustomVideoClient(
        {"id": "local-video", "request_model": "Wan", "base_url": "http://127.0.0.1:1/v1", "protocol": protocol, "api_key": ""}
    )


class StubResponse:
    status_code = 200
    text = "{}"

    def json(self):
        return {"id": "job-1", "status": "queued"}


# 1) 文件字段候选方案（vllm-omni 主形态优先）
c = make_client()
ref_specs = c._image_file_specs(None, None, [p1, p2])
check("参考图候选1：vllm-omni input_references×2", ref_specs[0] == [("input_references", p1), ("input_references", p2)], str(ref_specs[0]))
check("参考图候选2：同名 input_reference 回退", ref_specs[1] == [("input_reference", p1), ("input_reference", p2)])
check("参考图候选3：reference_images 回退", ref_specs[2][0][0] == "reference_images")
check("参考图候选4：image[] 回退", ref_specs[3][0][0] == "image[]")
se_specs = c._image_file_specs(p1, p2, None)
check("首尾帧候选1：vllm-omni input_references×2", se_specs[0] == [("input_references", p1), ("input_references", p2)], str(se_specs[0]))
check("首尾帧回退覆盖尾帧命名", [s[1][0] for s in se_specs[1:]] == ["last_frame", "last_image", "tail_image", "input_reference"], str([s[1][0] for s in se_specs[1:]]))
check("单图：input_reference", c._image_file_specs(p1, None, None) == [[("input_reference", p1)]])
check("纯文本：空方案", c._image_file_specs(None, None, None) == [[]])

# 2) 编码 × 字段组合（新签名：image_specs, task, duration → 三元组序列）
v_files = c._create_variants([[("input_reference", p1)]], "fl2va", 5)
check(
    "含文件仅 multipart（不再退化为无图 JSON）",
    all(enc == "multipart" for enc, _, _ in v_files),
    str([(e, f) for e, f, _ in v_files]),
)
v_text = c._create_variants([[]], "t2va", 5)
check("纯文本 vllm-omni：multipart → json", [e for e, _, _ in v_text] == ["multipart", "multipart", "json"], str([(e, x) for e, _, x in v_text]))
v_text_sg = make_client("sglang")._create_variants([[]], "t2va", 5)
check("纯文本 sglang：json → multipart", [e for e, _, _ in v_text_sg] == ["json", "multipart"], str([(e, x) for e, _, x in v_text_sg]))

# 3) multipart 组装（捕获 httpx 调用）
captured = {}


def fake_post(path, **kwargs):
    captured["path"] = path
    captured["kwargs"] = kwargs
    return StubResponse()


c._client.post = fake_post
c._post_create("multipart", "prompt-x", [("input_reference", p1), ("last_frame", p2)], "1280x720", 5, None, None)
files = captured["kwargs"].get("files")
check("multipart 字段顺序与命名", isinstance(files, list) and [f[0] for f in files] == ["input_reference", "last_frame"], str(files and [f[0] for f in files]))
check("multipart data 含 prompt/model/size/seconds",
      captured["kwargs"]["data"]["prompt"] == "prompt-x" and captured["kwargs"]["data"]["seconds"] == "5")

# 4) HTTP：ability 查询返回均为可用视频模型子集（动态，环境无关）
BASE = "http://127.0.0.1:8500"  # 容器内外同端口（.env BACKEND_PORT）


def get(url):
    with urllib.request.urlopen(BASE + url, timeout=30) as r:
        return json.loads(r.read().decode())


video_ids = {m["id"] for m in get("/api/models?model_type=video")["models"]}
check("video 类型列表非空", bool(video_ids), str(sorted(video_ids)))
for ability in ("first_frame_i2v", "start_end_frame_i2v", "reference_to_video", "audio_reference", "video_reference"):
    ids = {m["id"] for m in get(f"/api/models?media_type=video&ability={ability}&verified_only=true")["models"]}
    check(f"ability={ability} 返回均为可用视频模型子集", ids <= video_ids, str(sorted(ids)))

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)