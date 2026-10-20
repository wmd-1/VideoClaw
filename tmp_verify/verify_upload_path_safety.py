"""artifact-upload-path-safety 验证：标识白名单、realpath 预检/复核、零副作用与存量扫描。

运行：
docker exec -i -w /app -e PYTHONPATH=/app video-claw-backend \
  /app/.venv/bin/python - < tmp_verify/verify_upload_path_safety.py
"""
import json
import os
import shutil
import sys
import tempfile
import uuid

sys.path.insert(0, "/app")

from fastapi.testclient import TestClient  # noqa: E402

from api.app import app  # noqa: E402
from api.dependencies import workflow_engine  # noqa: E402
from config import settings  # noqa: E402
from core.orchestrator import (  # noqa: E402
    WorkflowEngine,
    _is_valid_upload_identifier,
    validate_upload_identifier,
)

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


def tree(root):
    if not os.path.isdir(root):
        return []
    entries = []
    for base, dirs, files in os.walk(root):
        for name in dirs:
            entries.append(("d", os.path.relpath(os.path.join(base, name), root)))
        for name in files:
            entries.append(("f", os.path.relpath(os.path.join(base, name), root)))
    return sorted(entries)


client = TestClient(app)

try:
    # ── 1. 标识格式矩阵 ────────────────────────────────────────────────
    invalid_values = ["../x", "a/b", "a\\b", "", "a b", "a\x00b", "x" * 65, ".", ".."]
    valid_values = ["1234567890123", "zz-draftmerge-e2e", "char_1", "set_2", "seg_01_01"]
    for value in invalid_values:
        check(f"非法标识被拒绝: {value!r}", expect_value_error(validate_upload_identifier, value, "item_id") is not None)
    for value in valid_values:
        check(f"合法标识放行: {value}", validate_upload_identifier(value, "item_id") == value)

    # ── 2. 读取/创建/保存/删除入口 ─────────────────────────────────────
    check("get_state 非法标识返回 None（不读盘）", workflow_engine.get_state("bad.id") is None)
    check("get_or_create_state 非法标识被拒绝", expect_value_error(workflow_engine.get_or_create_state, "bad.id") is not None)
    check("delete_session 非法标识返回 False", workflow_engine.delete_session("../x") is False)
    check("save_session_to_disk 非法标识被拒绝", expect_value_error(workflow_engine.save_session_to_disk, "../x") is not None)
    leaked = os.path.join(os.path.dirname(workflow_engine._session_dir), "x.json")
    check("save_session_to_disk 拒绝后无越界文件", not os.path.exists(leaked))

    # ── 3. 磁盘装载跳过非法 session_id ─────────────────────────────────
    bad_session_file = os.path.join(workflow_engine._session_dir, f"bad_{uuid.uuid4().hex[:6]}.json")
    with open(bad_session_file, "w", encoding="utf-8") as handle:
        json.dump({"session_id": "bad.id", "status": "pending", "artifacts": {}}, handle)
    try:
        fresh_engine = WorkflowEngine()
        check("装载入口跳过非法 session_id", "bad.id" not in fresh_engine.sessions)
    finally:
        os.remove(bad_session_file)

    # ── 4. 路径预检/复核（临时 RESULT_DIR） ────────────────────────────
    original_result_dir = settings.RESULT_DIR
    temp_root = tempfile.mkdtemp(prefix="verify_upload_")
    outside = tempfile.mkdtemp(prefix="verify_upload_outside_")
    settings.RESULT_DIR = temp_root
    sid = "verify123"
    try:
        os.makedirs(os.path.join(temp_root, "image"), exist_ok=True)
        os.symlink(outside, os.path.join(temp_root, "image", sid))
        msg = expect_value_error(WorkflowEngine._next_upload_path, sid, {"base": "char_x", "dir": "Scenes"}, ".png")
        check("会话目录为符号链接时预检拒绝", msg is not None and "越界" in msg, str(msg))
        check("预检拒绝后根外无新文件", tree(outside) == [])

        os.unlink(os.path.join(temp_root, "image", sid))
        shutil.rmtree(os.path.join(temp_root, "image"))
        os.symlink(outside, os.path.join(temp_root, "image"))
        msg = expect_value_error(WorkflowEngine._next_upload_path, sid, {"base": "char_x", "dir": "Scenes"}, ".png")
        check("image 根为符号链接时预检拒绝", msg is not None and "越界" in msg, str(msg))
        check("image 根符号链接时根外无新文件", tree(outside) == [])

        os.unlink(os.path.join(temp_root, "image"))
        abs_path, rel_path = WorkflowEngine._next_upload_path(sid, {"base": "char_x", "dir": "Scenes"}, ".png")
        check("普通目录正常写入", abs_path.startswith(os.path.join(temp_root, "image", sid)) and os.path.isdir(os.path.dirname(abs_path)))
        check("相对路径基于 BASE_DIR", not os.path.isabs(rel_path), rel_path)

        msg = expect_value_error(WorkflowEngine._next_upload_path, sid, {"base": "../../x", "dir": "Scenes"}, ".png")
        check("非法 base 被拒绝且不回显绝对路径", msg is not None and "RESULT_DIR" not in msg and temp_root not in msg, str(msg))
    finally:
        settings.RESULT_DIR = original_result_dir
        shutil.rmtree(temp_root, ignore_errors=True)
        shutil.rmtree(outside, ignore_errors=True)

    # ── 5. HTTP 合法上传与非法 item_id 零副作用 ───────────────────────
    session_id = f"verify{uuid.uuid4().hex[:8]}"
    item_id = f"char_{uuid.uuid4().hex[:8]}"
    workflow_engine.create_session(session_id, {})
    upload_root = os.path.join(settings.RESULT_DIR, "image", session_id)
    try:
        payload = b"\x89PNG\r\n\x1a\n" + b"0" * 16
        response = client.post(
            f"/api/project/{session_id}/artifact/character_design/upload_image",
            data={"item_type": "characters", "item_id": item_id},
            files={"file": ("verify.png", payload, "image/png")},
        )
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        versions = (body.get("artifact") or {}).get("characters", [{}])[0].get("versions", [])
        check("合法上传返回 200", response.status_code == 200, response.text[:160])
        check("合法上传追加 versions 且 selected 指向新文件", len(versions) == 1 and body.get("path") == versions[0], str(versions))

        before_tree = tree(upload_root)
        response = client.post(
            f"/api/project/{session_id}/artifact/character_design/upload_image",
            data={"item_type": "characters", "item_id": "../../evil"},
            files={"file": ("verify.png", payload, "image/png")},
        )
        check("非法 item_id 返回 400", response.status_code == 400, response.text[:160])
        check("非法 item_id 后上传目录树不变", tree(upload_root) == before_tree)

        response = client.post(
            "/api/project/bad.id/artifact/character_design/upload_image",
            data={"item_type": "characters", "item_id": item_id},
            files={"file": ("verify.png", payload, "image/png")},
        )
        check("非法 session_id 返回 400", response.status_code == 400, response.text[:160])

        response = client.get("/api/project/%2e%2e%2ftasks%2fx/status")
        check("编码路径探测不返回 200", response.status_code in (400, 404), str(response.status_code))

        response = client.post("/api/project/bad.id/execute/script_generation", json={})
        check("非法 session_id 的执行入口返回 4xx", response.status_code in (400, 404), response.text[:160])
    finally:
        workflow_engine.delete_session(session_id)

    # ── 6. 存量会话/条目 ID 扫描 ───────────────────────────────────────
    def collect_ids(value, acc):
        if isinstance(value, dict):
            if isinstance(value.get("id"), str):
                acc.append(value["id"])
            for item in value.values():
                collect_ids(item, acc)
        elif isinstance(value, list):
            for item in value:
                collect_ids(item, acc)

    bad_files = []
    bad_ids = []
    session_files = [
        name for name in os.listdir(workflow_engine._session_dir) if name.endswith(".json")
    ]
    for name in session_files:
        stem = name[:-5]
        if not _is_valid_upload_identifier(stem):
            bad_files.append(name)
        try:
            with open(os.path.join(workflow_engine._session_dir, name), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            continue
        ids = []
        collect_ids(data.get("artifacts"), ids)
        bad_ids.extend(value for value in ids if not _is_valid_upload_identifier(value))
    check("存量会话文件名命中白名单", not bad_files, str(bad_files[:5]))
    check("存量 artifact 条目 ID 命中白名单", not bad_ids, str(sorted(set(bad_ids))[:5]))

finally:
    # 确保测试会话已清理（若上面提前失败）
    try:
        if "session_id" in locals():
            workflow_engine.delete_session(session_id)
    except Exception:
        pass

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL PASS")
