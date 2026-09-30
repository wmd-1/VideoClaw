"""验证沙盒媒体参考路径白名单校验（C1 + I-A，评审加固）。

覆盖：
- C1（audio_url，经 _resolve_media_reference_url）：绝对路径越界/相对 `..` 逃逸/符号链接 → 拒绝，
  不复制到静态可访问目录；合法 TEMP_DIR 上传件仍可转换为 /code 绝对 URL；拒绝后 uploads 无新增；
- I-A（image / reference_videos，经 _guard_local_media）：白名单外绝对路径/`..` 逃逸/符号链接 →
  发起生成前拒绝，文件不被适配层打开与外传；合法 TEMP_DIR 上传件透传规范化绝对路径。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_sandbox_path_guard.py
"""
import io
import os
import sys
import uuid

sys.path.insert(0, "/app")
if os.environ.get("VC_PATCHED_BACKEND"):
    sys.path.insert(0, os.environ["VC_PATCHED_BACKEND"])
from fastapi.testclient import TestClient  # noqa: E402

from api.app import app  # noqa: E402
from config import settings  # noqa: E402

failures = []
SAVE_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"o" * 32
UPLOADS_DIR = os.path.join(settings.CODE_DIR, "result", "sandbox", "uploads")
CAPTURED = {}


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


class StubVideoClient:
    def generate_video(self, **kwargs):
        CAPTURED.clear()
        CAPTURED.update(kwargs)
        save_path = kwargs["save_path"]
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(SAVE_BYTES)
        return "stub://video"


def uploads_snapshot():
    if not os.path.isdir(UPLOADS_DIR):
        return set()
    return set(os.listdir(UPLOADS_DIR))


def patch_video_client():
    import models.video_client as vc_module

    original = vc_module.VideoClient
    vc_module.VideoClient = StubVideoClient
    return original


client = TestClient(app)
original = patch_video_client()

# 在白名单之外制造一个真实可读文件（绝对路径越界用例的目标）
outside_path = os.path.join("/tmp", f"vc_outside_{uuid.uuid4().hex}.wav")
with open(outside_path, "wb") as f:
    f.write(b"RIFF" + b"x" * 64)

try:
    before = uploads_snapshot()

    # 1) 绝对路径越界：拒绝且不复制
    CAPTURED.clear()
    r1 = client.post("/api/sandbox/video", json={
        "model": "local-video", "prompt": "abs escape", "audio_url": outside_path,
    }).json()
    check("绝对路径越界被拒绝", r1.get("success") is False and "越界" in (r1.get("error") or ""), str(r1)[:160])
    check("越界拒绝时不发起生成", not CAPTURED, str(CAPTURED)[:120])

    # 2) 相对路径 `..` 逃逸：拒绝且不返回 /code/../ 形态 URL
    CAPTURED.clear()
    rel_escape = os.path.relpath(outside_path, settings.CODE_DIR)
    r2 = client.post("/api/sandbox/video", json={
        "model": "local-video", "prompt": "dotdot escape", "audio_url": rel_escape,
    }).json()
    check("相对 `..` 逃逸被拒绝", r2.get("success") is False and "越界" in (r2.get("error") or ""), f"rel={rel_escape} resp={str(r2)[:160]}")
    check("逃逸拒绝时不发起生成", not CAPTURED, str(CAPTURED)[:120])

    # 3) TEMP_DIR 内符号链接指向外部：realpath 解析越界 → 拒绝
    os.makedirs(settings.TEMP_DIR, exist_ok=True)
    link_path = os.path.join(settings.TEMP_DIR, f"vc_link_{uuid.uuid4().hex}.wav")
    try:
        os.symlink(outside_path, link_path)
        CAPTURED.clear()
        r3 = client.post("/api/sandbox/video", json={
            "model": "local-video", "prompt": "symlink escape", "audio_url": link_path,
        }).json()
        check("符号链接逃逸被拒绝", r3.get("success") is False and "越界" in (r3.get("error") or ""), str(r3)[:160])
        check("符号链接拒绝时不发起生成", not CAPTURED, str(CAPTURED)[:120])
    finally:
        if os.path.islink(link_path):
            os.remove(link_path)

    # 4) 合法 TEMP_DIR 上传件：仍可转换为 /code 绝对 URL（回归）
    up = client.post(
        "/api/upload_media",
        files={"file": ("good.wav", io.BytesIO(b"RIFF" + b"a" * 64), "audio/wav")},
    )
    check("上传合法音频成功", up.status_code == 200 and up.json().get("file_path"), up.text[:120])
    good_path = up.json()["file_path"]
    CAPTURED.clear()
    base = str(client.base_url).rstrip("/")
    r4 = client.post("/api/sandbox/video", json={
        "model": "local-video", "prompt": "legit audio", "audio_url": good_path,
    }).json()
    resolved = CAPTURED.get("audio_reference_url") or ""
    check("合法上传件转换为 /code URL", r4.get("success") and resolved.startswith(base + "/code/"), f"url={resolved} resp={str(r4)[:120]}")

    # 5) 三个越界用例均未向 uploads 目录落地（仅合法件+1）
    after = uploads_snapshot()
    added = after - before
    check("越界路径未造成落地扩散", len(added) <= 1, f"added={sorted(added)}")

    # ===== I-A：image / reference_videos 统一守卫 =====
    outside_img = os.path.join("/tmp", f"vc_outside_img_{uuid.uuid4().hex}.png")
    with open(outside_img, "wb") as f:
        f.write(b"\x89PNG" + b"p" * 64)
    outside_vid = os.path.join("/tmp", f"vc_outside_vid_{uuid.uuid4().hex}.mp4")
    with open(outside_vid, "wb") as f:
        f.write(SAVE_BYTES)
    try:
        # 6) 绝对路径越界的 image：拒绝且无生成
        CAPTURED.clear()
        r6 = client.post("/api/sandbox/video", json={
            "model": "local-video", "prompt": "image escape", "image": outside_img,
        }).json()
        check("image 绝对路径越界被拒绝", r6.get("success") is False and "越界" in (r6.get("error") or ""), str(r6)[:160])
        check("image 越界拒绝时不发起生成", not CAPTURED, str(CAPTURED)[:120])

        # 7) 绝对路径越界的 reference_videos：拒绝且无生成（不得进入 open+外传链路）
        CAPTURED.clear()
        r7 = client.post("/api/sandbox/video", json={
            "model": "local-video", "prompt": "video escape", "reference_videos": [outside_vid],
        }).json()
        check("reference_videos 越界被拒绝", r7.get("success") is False and "越界" in (r7.get("error") or ""), str(r7)[:160])
        check("reference_videos 越界拒绝时不发起生成", not CAPTURED, str(CAPTURED)[:120])

        # 8) `..` 逃逸的 image：拒绝
        CAPTURED.clear()
        r8 = client.post("/api/sandbox/video", json={
            "model": "local-video", "prompt": "image dotdot",
            "image": os.path.relpath(outside_img, settings.CODE_DIR),
        }).json()
        check("image 相对 `..` 逃逸被拒绝", r8.get("success") is False and "越界" in (r8.get("error") or ""), str(r8)[:160])

        # 9) TEMP_DIR 内符号链接作 image：realpath 解析越界 → 拒绝
        link_img = os.path.join(settings.TEMP_DIR, f"vc_img_link_{uuid.uuid4().hex}.png")
        os.symlink(outside_img, link_img)
        try:
            CAPTURED.clear()
            r9 = client.post("/api/sandbox/video", json={
                "model": "local-video", "prompt": "image symlink", "image": link_img,
            }).json()
            check("image 符号链接逃逸被拒绝", r9.get("success") is False and "越界" in (r9.get("error") or ""), str(r9)[:160])
        finally:
            if os.path.islink(link_img):
                os.remove(link_img)

        # 10) 合法 TEMP_DIR 上传件：image/视频参考正常透传（规范化绝对路径）
        img_up = client.post("/api/upload_media", files={"file": ("ok.png", io.BytesIO(b"\x89PNG" + b"q" * 64), "image/png")}).json()
        vid_up = client.post("/api/upload_media", files={"file": ("ok.mp4", io.BytesIO(SAVE_BYTES), "video/mp4")}).json()
        CAPTURED.clear()
        r10 = client.post("/api/sandbox/video", json={
            "model": "local-video", "prompt": "legit refs",
            "image": img_up["file_path"], "reference_videos": [vid_up["file_path"]],
        }).json()
        check("合法上传件 image 透传", r10.get("success") and CAPTURED.get("image_path") == os.path.realpath(img_up["file_path"]),
              f"image_path={CAPTURED.get('image_path')}")
        check("合法上传件 reference_videos 透传", CAPTURED.get("reference_video_paths") == [os.path.realpath(vid_up["file_path"])],
              str(CAPTURED.get("reference_video_paths")))
    finally:
        for p in (outside_img, outside_vid):
            if os.path.exists(p):
                os.remove(p)
finally:
    import models.video_client as vc_module

    vc_module.VideoClient = original
    if os.path.isfile(outside_path):
        os.remove(outside_path)

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)
