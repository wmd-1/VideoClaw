"""pipeline-input-path-guard 验证：共享守卫、pipeline 入口、文档路径与 /code 暴露面。

运行：
docker exec -i -w /app -e PYTHONPATH=/app video-claw-backend \
  /app/.venv/bin/python - < tmp_verify/verify_pipeline_path_guard.py
"""
import json
import os
import shutil
import sys
import time
import uuid

sys.path.insert(0, "/app")

from fastapi.testclient import TestClient  # noqa: E402

import api.media_paths as mp  # noqa: E402
from api.app import app  # noqa: E402
from api.routers.files import merge_uploaded_file_into_idea  # noqa: E402
from api.routers.pipelines import _guard_pipeline_media_params  # noqa: E402
from config import settings  # noqa: E402
from pipelines.utils import copy_input_file  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


def expect_value_error(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ValueError as exc:
        return str(exc)
    return None


def write_bytes(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


CODE_DIR = os.path.realpath(str(settings.CODE_DIR))
RESULT_DIR = os.path.realpath(str(settings.RESULT_DIR))
TEMP_DIR = os.path.realpath(str(settings.TEMP_DIR))
TASK_DIR = os.path.join(RESULT_DIR, "task")

tag = uuid.uuid4().hex[:8]
ok_result = write_bytes(os.path.join(RESULT_DIR, "verify_guard", f"{tag}.png"), b"png")
ok_temp = write_bytes(os.path.join(TEMP_DIR, f"verify_guard_{tag}.png"), b"png")
bad_root = write_bytes(os.path.join(CODE_DIR, f"verify_guard_root_{tag}.png"), b"png")
bad_data = write_bytes(os.path.join(CODE_DIR, "data", f"verify_guard_{tag}.json"), b"{}")
outside = write_bytes(f"/tmp/verify_guard_outside_{tag}.png", b"png")
link_out = os.path.join(RESULT_DIR, "verify_guard", f"link_{tag}.png")
if os.path.lexists(link_out):
    os.remove(link_out)
os.symlink(outside, link_out)

client = TestClient(app)
tmpdir = None
static_ok = None


def task_count():
    return len(client.get("/api/tasks").json().get("tasks", []))


def task_dirs():
    if not os.path.isdir(TASK_DIR):
        return set()
    return set(os.listdir(TASK_DIR))


def wait_task(task_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/tasks/{task_id}").json()
        if data.get("status") in ("completed", "failed"):
            return data
        time.sleep(0.1)
    return client.get(f"/api/tasks/{task_id}").json()


try:
    # ── 1. 共享守卫单元矩阵 ─────────────────────────────────────────────
    check("允许根内 result 文件放行", mp.guard_media_input(ok_result, "f") == os.path.realpath(ok_result))
    check("允许根内 TEMP 文件放行", mp.guard_media_input(ok_temp, "f") == os.path.realpath(ok_temp))
    err = expect_value_error(mp.guard_media_input, "/etc/passwd", "f")
    check("/etc/passwd 被拒绝且原因含越界", err is not None and "越界" in err, str(err))
    err = expect_value_error(mp.guard_media_input, "/app/config.yaml", "f")
    check("/app/config.yaml 被拒绝", err is not None and "越界" in err, str(err))
    err = expect_value_error(mp.guard_media_input, bad_root, "f")
    check("CODE_DIR 顶层文件被拒绝（根已收窄）", err is not None and "越界" in err, str(err))
    err = expect_value_error(mp.guard_media_input, bad_data, "f")
    check("code/data 元数据被拒绝", err is not None and "越界" in err, str(err))
    err = expect_value_error(mp.guard_media_input, "file:///etc/passwd", "f")
    check("file:///etc/passwd 被拒绝", err is not None and "越界" in err, str(err))
    check(
        "file:// 白名单内文件剥前缀为裸路径",
        mp.guard_media_input(f"file://{ok_result}", "f") == os.path.realpath(ok_result),
    )
    err = expect_value_error(mp.guard_media_input, "../../etc/passwd", "f")
    check("相对路径 .. 逃逸被拒绝", err is not None and "越界" in err, str(err))
    err = expect_value_error(mp.guard_media_input, link_out, "f")
    check("符号链接逃逸被拒绝", err is not None and "越界" in err, str(err))
    url = "HTTP://example.com/a.png"
    check("HTTP:// 大小写不敏感透传", mp.guard_media_input(url, "f") == url)
    check("https URL 透传", mp.guard_media_input("https://example.com/a.png", "f").startswith("https://"))
    check("data URL 透传", mp.guard_media_input("data:image/png;base64,QQ==", "f").startswith("data:"))
    check("oss URL 透传", mp.guard_media_input("oss://bucket/a.png", "f").startswith("oss://"))
    err = expect_value_error(mp.guard_media_input, "ftp://example.com/a.png", "f")
    check("未登记协议被拒绝且提示支持范围", err is not None and "不支持" in err and "http" in err, str(err))

    # ── 2. 上传文档名守卫 ───────────────────────────────────────────────
    check("合法上传文件名放行", mp.guard_uploaded_document_name("1700000000_doc.txt") == "1700000000_doc.txt")
    err = expect_value_error(mp.guard_uploaded_document_name, "/etc/passwd")
    check("文档绝对路径被拒绝", err is not None and "相对文件名" in err, str(err))
    err = expect_value_error(mp.guard_uploaded_document_name, "../../app/README.md")
    check("文档相对逃逸被拒绝", err is not None and "相对文件名" in err, str(err))
    err = expect_value_error(mp.guard_uploaded_document_name, "sub/doc.txt")
    check("文档含分隔符被拒绝", err is not None and "相对文件名" in err, str(err))

    # ── 3. copy_input_file 第二道防线 ───────────────────────────────────
    tmpdir = os.path.join(TEMP_DIR, f"verify_copy_{tag}")
    os.makedirs(tmpdir, exist_ok=True)
    err = expect_value_error(copy_input_file, "/app/config.yaml", tmpdir, "x")
    check("copy_input_file 拒绝越界路径且目录为空", err is not None and os.listdir(tmpdir) == [], str(err))
    check("copy_input_file 透传 https", copy_input_file("https://example.com/a.png", tmpdir, "x").startswith("https://"))
    check("copy_input_file 透传 oss", copy_input_file("oss://bucket/a.png", tmpdir, "x").startswith("oss://"))
    copied = copy_input_file(f"file://{ok_result}", tmpdir, "input")
    check("copy_input_file file:// 用守卫返回值复制", os.path.isfile(copied) and open(copied, "rb").read() == b"png")

    # ── 4. 别名键（纵深防御，单元级） ───────────────────────────────────
    err = expect_value_error(_guard_pipeline_media_params, {"image_asset": "/etc/passwd"})
    check("别名 image_asset 被守卫覆盖", err is not None and "越界" in err, str(err))
    err = expect_value_error(_guard_pipeline_media_params, {"character_asset": "../../etc/passwd"})
    check("别名 character_asset 被守卫覆盖", err is not None and "越界" in err, str(err))

    # ── 5. HTTP 越界拒绝：无任务、无目录副作用 ──────────────────────────
    before_count, before_dirs = task_count(), task_dirs()
    reject_cases = [
        ("/api/pipelines/action_transfer/tasks", {"prompt_text": "p", "image_path": "/etc/passwd", "video_path": ok_temp, "video_model": "m"}),
        ("/api/pipelines/action_transfer/tasks", {"prompt_text": "p", "image_path": "file:///app/config.yaml", "video_path": ok_temp, "video_model": "m"}),
        ("/api/pipelines/action_transfer/tasks", {"prompt_text": "p", "image_path": bad_data, "video_path": ok_temp, "video_model": "m"}),
        ("/api/pipelines/action_transfer/tasks", {"prompt_text": "p", "image_path": "../../etc/passwd", "video_path": ok_temp, "video_model": "m"}),
        ("/api/pipelines/action_transfer/tasks", {"prompt_text": "p", "image_path": link_out, "video_path": ok_temp, "video_model": "m"}),
        ("/api/pipelines/digital_human/tasks", {"mode": "customize", "character_image_path": "/etc/passwd", "goods_text": "x", "llm_model": "m", "image_model": "m", "video_model": "m"}),
        ("/api/pipelines/quick_create/tasks", {"params": {"image_path": "/etc/passwd"}}),
    ]
    for url, payload in reject_cases:
        response = client.post(url, json=payload)
        check(f"HTTP 拒绝越界: {url}", response.status_code == 400 and "越界" in response.text, f"{response.status_code} {response.text[:120]}")
    check("拒绝后任务数不变", task_count() == before_count)
    check("拒绝后任务目录不变", task_dirs() == before_dirs)

    # ── 6. project start 的 file_path ───────────────────────────────────
    for bad_path in ("/etc/passwd", "/app/README.md", "../../app/README.md"):
        response = client.post("/api/project/start", json={"idea": "x", "file_path": bad_path})
        check(f"project start 拒绝非法 file_path: {bad_path}", response.status_code == 400, f"{response.status_code} {response.text[:120]}")
    doc_name = f"verify_{tag}_doc.txt"
    doc_path = write_bytes(os.path.join(TEMP_DIR, doc_name), "文档正文".encode("utf-8"))
    merged = merge_uploaded_file_into_idea("idea", doc_name)
    check("合法上传文件名仍注入内容", "文档正文" in merged, merged[:80])
    os.remove(doc_path)

    # ── 7. 合法任务回归（mock 适配层，不触真实模型） ────────────────────
    import pipelines.action_transfer as action_transfer

    original_action = action_transfer.generate_video_api

    def stub_action(**kwargs):
        write_bytes(kwargs["output_path"], b"fakevideo")
        return kwargs["output_path"]

    action_transfer.generate_video_api = stub_action
    created_tasks = []
    try:
        response = client.post("/api/pipelines/action_transfer/tasks", json={
            "prompt_text": "p", "image_path": ok_result, "video_path": ok_temp, "video_model": "m",
        })
        check("action_transfer 合法输入任务创建", response.status_code == 200, response.text[:120])
        if response.status_code == 200:
            payload = response.json()
            created_tasks.append(payload["task_id"])
            data = wait_task(payload["task_id"])
            check("action_transfer 任务完成", data.get("status") == "completed", str(data.get("status")))
            check("action_transfer request.json 生成", os.path.isfile(os.path.join(payload["output_dir"], "request.json")))
            check("action_transfer 输入件复制到任务目录", os.path.isfile(os.path.join(payload["output_dir"], "input_image.png")))
    finally:
        action_transfer.generate_video_api = original_action

    import pipelines.digital_human as digital_human

    originals = {
        "LLM": digital_human.LLM,
        "generate_edge_tts": digital_human.generate_edge_tts,
        "generate_video_api": digital_human.generate_video_api,
        "media_duration_seconds": digital_human.media_duration_seconds,
        "replace_video_audio": digital_human.replace_video_audio,
        "parse_api_workflow": digital_human.parse_api_workflow,
        "video_capabilities": digital_human.video_capabilities,
    }

    class StubLLM:
        def query(self, *args, **kwargs):
            return "标题"

    async def stub_tts(*args, **kwargs):
        write_bytes(kwargs["output_path"], b"audio")

    def stub_video(**kwargs):
        write_bytes(kwargs["output_path"], b"video")
        return kwargs["output_path"]

    def stub_replace(video_path, audio_path, output_path):
        shutil.copy2(video_path, output_path)
        return output_path

    digital_human.LLM = StubLLM
    digital_human.generate_edge_tts = stub_tts
    digital_human.generate_video_api = stub_video
    digital_human.media_duration_seconds = lambda _path: 1.0
    digital_human.replace_video_audio = stub_replace
    digital_human.parse_api_workflow = lambda model, _kind: ("", model)
    digital_human.video_capabilities = lambda *_args, **_kwargs: {}
    try:
        response = client.post("/api/pipelines/digital_human/tasks", json={
            "mode": "customize", "character_image_path": ok_result, "goods_text": "你好。",
            "llm_model": "m", "image_model": "m", "video_model": "m",
        })
        check("digital_human 合法输入任务创建", response.status_code == 200, response.text[:120])
        if response.status_code == 200:
            payload = response.json()
            created_tasks.append(payload["task_id"])
            data = wait_task(payload["task_id"])
            check("digital_human 任务完成", data.get("status") == "completed", str(data.get("status")))
            check("digital_human request.json 生成", os.path.isfile(os.path.join(payload["output_dir"], "request.json")))
    finally:
        for key, value in originals.items():
            setattr(digital_human, key, value)

    # ── 8. /code 暴露面 ─────────────────────────────────────────────────
    static_ok = write_bytes(os.path.join(RESULT_DIR, "verify_guard", f"static_{tag}.txt"), b"ok")
    response = client.get(f"/code/result/verify_guard/static_{tag}.txt")
    check("/code/result 媒体可访问", response.status_code == 200 and response.content == b"ok", f"{response.status_code}")
    check("/code/data/sessions 不可匿名读取", client.get("/code/data/sessions/x.json").status_code == 404)
    check("/code/data/tasks 不可匿名读取", client.get("/code/data/tasks/x.json").status_code == 404)
    check("/code 不再暴露 CODE_DIR 顶层文件", client.get(f"/code/{os.path.basename(bad_root)}").status_code == 404)

    # ── 9. 清理创建的测试任务 ───────────────────────────────────────────
    for task_id in created_tasks:
        client.delete(f"/api/tasks/{task_id}")
finally:
    for path in (ok_result, ok_temp, bad_root, bad_data, outside, link_out, static_ok):
        try:
            if path and os.path.lexists(path):
                os.remove(path)
        except OSError:
            pass
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)
    shutil.rmtree(os.path.join(RESULT_DIR, "verify_guard"), ignore_errors=True)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL PASS")
