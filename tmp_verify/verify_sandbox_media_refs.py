"""验证沙盒视频 API 的媒体参考：Schema 扩展 + 本地文件 → 服务端可访问 URL 转换。

覆盖：
- audio_url 为 HTTP(S) URL：原样透传；
- audio_url 为本地上传文件（/api/upload_media）：转换为 {request.base_url}code/... 绝对 URL 且文件可达；
- audio_url 无法定位：明确报错（不静默丢弃）；
- reference_videos：本地视频路径透传至 generate_video（reference_video_paths）；
- 未提供媒体参考：请求字段缺省（向后兼容）。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_sandbox_media_refs.py
"""
import io
import os
import sys
import uuid

sys.path.insert(0, "/app")
# 测试辅助：通过 VC_PATCHED_BACKEND 优先加载补丁代码副本（镜像重建后无需该变量）
if os.environ.get("VC_PATCHED_BACKEND"):
    sys.path.insert(0, os.environ["VC_PATCHED_BACKEND"])
from fastapi.testclient import TestClient  # noqa: E402

from api.app import app  # noqa: E402
from config import settings  # noqa: E402

failures = []
CAPTURED = {}
SAVE_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"o" * 32


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


class StubVideoClient:
    """捕获 generate_video 入参；生成产物写入 save_path。"""

    def generate_video(self, **kwargs):
        CAPTURED.clear()
        CAPTURED.update(kwargs)
        save_path = kwargs["save_path"]
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(SAVE_BYTES)
        return "stub://video"


def patch_video_client():
    import models.video_client as vc_module

    original = vc_module.VideoClient
    vc_module.VideoClient = StubVideoClient
    return original


client = TestClient(app)
original = patch_video_client()
try:
    # 准备：上传一个音频文件与一个视频文件（走真实 /api/upload_media）
    audio_resp = client.post(
        "/api/upload_media",
        files={"file": ("ref.wav", io.BytesIO(b"RIFF" + b"a" * 64), "audio/wav")},
    )
    check("上传音频成功", audio_resp.status_code == 200 and audio_resp.json().get("file_path"), audio_resp.text[:120])
    audio_path = audio_resp.json()["file_path"]

    video_resp = client.post(
        "/api/upload_media",
        files={"file": ("ref.mp4", io.BytesIO(SAVE_BYTES), "video/mp4")},
    )
    check("上传视频成功", video_resp.status_code == 200 and video_resp.json().get("file_path"), video_resp.text[:120])
    video_path = video_resp.json()["file_path"]

    base = str(client.base_url).rstrip("/")

    # 1) HTTP(S) 音频 URL：原样透传
    r1 = client.post("/api/sandbox/video", json={
        "model": "local-video", "prompt": "lip-sync demo",
        "audio_url": "https://example.com/ref.wav",
    }).json()
    check("HTTP URL 原样透传", r1.get("success") and CAPTURED.get("audio_reference_url") == "https://example.com/ref.wav", str(r1)[:160])

    # 2) 本地上传音频：转换为可访问绝对 URL（/code 静态目录内）
    r2 = client.post("/api/sandbox/video", json={
        "model": "local-video", "prompt": "lip-sync demo", "audio_url": audio_path,
    }).json()
    resolved = CAPTURED.get("audio_reference_url") or ""
    check("本地音频转换为绝对 URL", r2.get("success") and resolved.startswith(base + "/code/"), f"url={resolved} resp={str(r2)[:120]}")
    check("URL 指向 uploads 目录", "/result/sandbox/uploads/" in resolved, resolved)
    local_mapped = resolved.split("/code/", 1)[1]
    check("URL 对应文件可达", os.path.isfile(os.path.join(settings.CODE_DIR, local_mapped)), local_mapped)

    # 3) reference_videos：透传至 reference_video_paths
    r3 = client.post("/api/sandbox/video", json={
        "model": "local-video", "prompt": "multi video refs",
        "reference_videos": [video_path],
    }).json()
    check("参考视频透传为 reference_video_paths", r3.get("success") and CAPTURED.get("reference_video_paths") == [video_path], str(r3)[:160])
    check("未提供音频参考时 audio_reference_url 为空", CAPTURED.get("audio_reference_url") in (None, ""), str(CAPTURED.get("audio_reference_url")))

    # 4) 无法定位的音频路径：明确报错
    CAPTURED.clear()
    r4 = client.post("/api/sandbox/video", json={
        "model": "local-video", "prompt": "bad audio", "audio_url": f"/nonexistent/{uuid.uuid4().hex}.wav",
    }).json()
    check("音频参考无法定位：明确报错", r4.get("success") is False and "不存在" in (r4.get("error") or ""), str(r4)[:160])
    check("报错时不发起生成请求", not CAPTURED, str(CAPTURED)[:120])

    # 5) 未提供媒体参考：参数缺省（向后兼容）
    r5 = client.post("/api/sandbox/video", json={
        "model": "local-video", "prompt": "plain t2v",
    }).json()
    check("无媒体参考：向后兼容", r5.get("success") and CAPTURED.get("audio_reference_url") in (None, "") and CAPTURED.get("reference_video_paths") is None, str(r5)[:160])
finally:
    import models.video_client as vc_module

    vc_module.VideoClient = original

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)
