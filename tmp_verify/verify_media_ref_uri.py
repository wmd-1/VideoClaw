"""sglang 媒体引用可达性验证：HTTP 引用优先、conditions 首选形态、不可达时的可执行提示。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_media_ref_uri.py
"""
import os
import sys

sys.path.insert(0, "/app")
import config as config_module  # noqa: E402
from config import Config  # noqa: E402
from models.custom_video import CustomVideoClient  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


CODE_DIR = str(Config.CODE_DIR)
inside = os.path.join(CODE_DIR, "result", "verify_media_ref_uri.png")
os.makedirs(os.path.dirname(inside), exist_ok=True)
open(inside, "wb").write(b"\x89PNG\r\n\x1a\n" + b"0" * 24)
outside = "/tmp/verify_media_ref_outside.png"
open(outside, "wb").write(b"\x89PNG\r\n\x1a\n" + b"0" * 24)


def client(protocol="sglang"):
    return CustomVideoClient({"id": "m", "request_model": "MiniMaxAI/MiniMax-H3", "base_url": "http://127.0.0.1:1/v1", "protocol": protocol, "api_key": ""})


# 1) 未配置基址：只有 file://（同机行为不变）
Config.MEDIA_REF_BASE_URL = ""
uris = client()._reference_uris(inside)
check("未配置基址时仅 file:// 形态", len(uris) == 1 and uris[0].startswith("file:///"), str(uris))

# 2) 配置基址：HTTP 在前、file:// 兜底；CODE_DIR 外文件不生成 HTTP
Config.MEDIA_REF_BASE_URL = "http://10.0.0.9:8500/"
uris = client()._reference_uris(inside)
relative = os.path.relpath(os.path.realpath(inside), os.path.realpath(CODE_DIR)).replace(os.sep, "/")
check("配置基址后 HTTP 优先且 file:// 兜底", uris == [f"http://10.0.0.9:8500/code/{relative}", "file://" + os.path.realpath(inside)], str(uris))
check("基址末尾斜杠被归一", "8500//code" not in uris[0], uris[0])
check("CODE_DIR 外文件不生成 HTTP 引用", client()._reference_uris(outside) == ["file://" + os.path.realpath(outside)], str(client()._reference_uris(outside)))
check("形态数量随可达性变化", client()._reference_uri_forms(inside) == 2 and client()._reference_uri_forms(outside) == 1)

# 3) conditions 变体按 uri_index 切换形态
c = client()
v_http = c._sglang_conditions_variant(inside, None, [], "fl2va", uri_index=0)
v_file = c._sglang_conditions_variant(inside, None, [], "fl2va", uri_index=1)
check("uri_index=0 用 HTTP 引用", v_http[2]["conditions"][0]["uri"].startswith("http://"), str(v_http[2]["conditions"]))
check("uri_index=1 退回 file://", v_file[2]["conditions"][0]["uri"].startswith("file://"), str(v_file[2]["conditions"]))
check("keyframe 角色与 frame_index 保持", v_http[2]["conditions"][0]["role"] == "keyframe" and v_http[2]["conditions"][0]["frame_index"] == 0, str(v_http[2]["conditions"]))
c._adjustments.clear()

# 4) 变体顺序：sglang 含图时 conditions 为首选（手册 L638：通用上传字段不会被 lowered 进 H3 参考条件）
captured = []


class StubResponse:
    def __init__(self, status_code=500, payload=None, text="boom"):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def make_post(status_code):
    def post(path, **kwargs):
        captured.append({"path": path, **kwargs})
        return StubResponse(status_code=status_code, payload={"id": "job-1", "status": "queued"})
    return post


Config.MEDIA_REF_BASE_URL = "http://10.0.0.9:8500"
order_client = client()
order_client._client.post = make_post(500)
map_fields = order_client._map_protocol_params(duration=5, video_ratio="16:9", resolution="768P", fps=None, short_edge=None, has_image=True)
try:
    order_client._create_job(prompt="p", image_path=inside, last_image_path=None, reference_image_paths=None,
                             size="1344x768", duration=5, negative_prompt=None, seed=None,
                             protocol_fields=map_fields[0], json_fields=map_fields[1], drop_size=map_fields[2])
except RuntimeError:
    pass
first = captured[0]
check("首选形态为 JSON+conditions（HTTP uri）", "conditions" in first.get("json", {}) and first["json"]["conditions"][0]["uri"].startswith("http://"), str(sorted(first.get("json", {}))))
check("第二种形态为 conditions+file://", len(captured) >= 2 and captured[1]["json"]["conditions"][0]["uri"].startswith("file://"), str(sorted(captured[1].get("json", {}))))
check("multipart 形态仍保留在后面", any("files" in body or "data" in body for body in captured[2:]), str([list(b)[:3] for b in captured]))
check("target 与 conditions 同时下发（含图 aspect_ratio=auto）", captured[0]["json"].get("target", {}).get("short_edge") == 768 and captured[0]["json"]["target"]["aspect_ratio"] == "auto", str(captured[0]["json"].get("target")))
Config.MEDIA_REF_BASE_URL = ""
captured.clear()
plain_client = client()
plain_client._client.post = make_post(500)
try:
    plain_client._create_job(prompt="p", image_path=inside, last_image_path=None, reference_image_paths=None,
                             size="1344x768", duration=5, negative_prompt=None, seed=None)
except RuntimeError:
    pass
check("未配置基址时 conditions 仅一种形态（file://）", len(captured) >= 1 and captured[0]["json"]["conditions"][0]["uri"].startswith("file://"), str(len(captured)))

# 5) vllm-omni 与 openai 的顺序不受影响（回归护栏）
for proto in ("vllm-omni", "openai"):
    captured.clear()
    other = client(proto)
    other._client.post = make_post(500)
    try:
        other._create_job(prompt="p", image_path=inside, last_image_path=None, reference_image_paths=None,
                          size="1344x768", duration=5, negative_prompt=None, seed=None)
    except RuntimeError:
        pass
    check(f"{proto} 含图仍走 multipart 主形态", "files" in captured[0] or "data" in captured[0], str(list(captured[0])))
    check(f"{proto} 不注入 sglang conditions 变体", not any("conditions" in (b.get("json") or {}) for b in captured), "")

# 6) 不可达引用的可执行提示
hint_unconfig = CustomVideoClient._reference_hint("HTTP 400: source does not exist or is not a file: /app/code/result/x.jpg")
check("未配置基址时提示给出 VC_MEDIA_REF_BASE_URL", "VC_MEDIA_REF_BASE_URL" in hint_unconfig and "file://" in hint_unconfig, hint_unconfig[:120])
Config.MEDIA_REF_BASE_URL = "http://10.0.0.9:8500"
hint_config = CustomVideoClient._reference_hint("HTTP 400: source does not exist or is not a file")
check("已配置基址时提示检查可达性而非改配置", "10.0.0.9:8500" in hint_config and "VC_MEDIA_REF_BASE_URL" not in hint_config, hint_config[:120])
check("无关错误不追加提示", CustomVideoClient._reference_hint("HTTP 400: target is required") == "")
Config.MEDIA_REF_BASE_URL = ""

# 清理
for path in (inside, outside):
    try:
        os.remove(path)
    except OSError:
        pass

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)