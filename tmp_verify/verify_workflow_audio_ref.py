"""验证工作流会话级音频参考透传：audio_reference_url → VideoClient.generate_video。

覆盖：
- 会话参数含 audio_reference_url：透传至视频生成调用；
- 会话参数不含该字段：不透传（缺省与现状完全一致）；
- reference_video_paths 按既有语义透传（参数映射断言）。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_workflow_audio_ref.py
"""
import os
import sys

sys.path.insert(0, "/app")
# 测试辅助：通过 VC_PATCHED_BACKEND 优先加载补丁代码副本（镜像重建后无需该变量）
if os.environ.get("VC_PATCHED_BACKEND"):
    sys.path.insert(0, os.environ["VC_PATCHED_BACKEND"])
from core.agents.video_agent import VideoDirectorAgent  # noqa: E402

failures = []
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
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42" + b"w" * 16)
        return "stub://video"


def patch():
    import models.video_client as vc_module

    original = vc_module.VideoClient
    vc_module.VideoClient = StubVideoClient
    return original


img = "/tmp/wf_audio_ref_img.png"
with open(img, "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 16)

original = patch()
try:
    agent = VideoDirectorAgent()

    # 1) 含 audio_reference_url：透传至视频生成调用
    CAPTURED.clear()
    _, path1, _ = agent._generate_one(
        "wf-audio-demo", "seg-01", "person speaks to camera", img,
        "local-video", duration=5, video_generation_mode="first_frame",
        llm_model="", audio_reference_url="https://example.com/ref.wav",
    )
    check("含参数：audio_reference_url 透传", CAPTURED.get("audio_reference_url") == "https://example.com/ref.wav", str(CAPTURED.get("audio_reference_url")))
    check("含参数：产物落盘", path1 and os.path.exists(path1), str(path1))

    # 2) 不含该字段：不透传（向后兼容，与现状一致）
    CAPTURED.clear()
    _, path2, _ = agent._generate_one(
        "wf-audio-demo", "seg-02", "plain first-frame generation", img,
        "local-video", duration=5, video_generation_mode="first_frame",
        llm_model="",
    )
    check("缺省：audio_reference_url 为 None（不注入字段）", CAPTURED.get("audio_reference_url") in (None, ""), str(CAPTURED.get("audio_reference_url")))
    check("缺省：链路正常", path2 and os.path.exists(path2), str(path2))

    # 3) reference_video_paths 按既有语义透传（参数映射）
    CAPTURED.clear()
    agent._generate_one(
        "wf-audio-demo", "seg-03", "reference video mode", None,
        "local-video", duration=5, video_generation_mode="reference",
        reference_image_paths=[img], llm_model="",
    )
    check("参考模式：reference_image_paths 透传", CAPTURED.get("reference_image_paths") == [img], str(CAPTURED.get("reference_image_paths")))
finally:
    import models.video_client as vc_module

    vc_module.VideoClient = original

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)
