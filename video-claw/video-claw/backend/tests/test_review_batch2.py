"""第六轮报告第二批回归：R01（上传剧本导入的 owner 校验）。

与 test_user_space.py 相同，只允许在既有后端容器内执行：
  docker exec -i -e PYTHONPATH=/app -w /app video-claw-backend \\
    /app/.venv/bin/python -m pytest /tmp/vc_tests -q
"""
from __future__ import annotations

import asyncio
import os
import shutil

import pytest

from api.dependencies import workflow_engine
from config import settings
from core.agents.script_agent import ScriptWriterAgent
from core.identity import owner_key

MODELS = {
    "llm_model": "test-llm",
    "vlm_model": "test-vlm",
    "image_t2i_model": "test-t2i",
    "image_it2i_model": "test-i2i",
    "video_first_frame_model": "test-video",
}


def headers(login: str) -> dict[str, str]:
    return {"X-User-Login-Name": login}


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    """独占上传根：先隔离 TEMP_DIR，再在其中造文件；清理只作用于该根。

    不能在真实 TEMP_DIR/users/<owner_key>/ 下造文件——那里的 rmtree 会连带删掉
    存量用户上传；测试库与测试桶都保护不了本地目录。
    """
    root = tmp_path / "temp"
    monkeypatch.setattr(settings, "TEMP_DIR", str(root))
    created: list[str] = []

    def make(login: str, name: str, content: str = "剧本正文") -> str:
        owner_dir = root / "users" / owner_key(login)
        owner_dir.mkdir(parents=True, exist_ok=True)
        path = owner_dir / name
        path.write_text(content, encoding="utf-8")
        resolved = os.path.realpath(str(path))
        assert resolved.startswith(os.path.realpath(str(root)) + os.sep), "上传文件必须落在独占根内"
        created.append(resolved)
        return f"users/{owner_key(login)}/{name}"

    yield make
    for path in created:
        if os.path.isfile(path):
            os.remove(path)
    shutil.rmtree(root, ignore_errors=True)


def test_r01_project_start_rejects_foreign_upload(client, session_dir, uploads):
    foreign = uploads("bob", "bob-script.txt", "bob 的剧本")
    response = client.post(
        "/api/project/start",
        headers=headers("alice"),
        json={"idea": "", "script_source": "upload", "file_path": foreign, **MODELS},
    )
    assert response.status_code == 400
    assert "当前用户" in response.json()["detail"]
    # 被拒绝时既不建会话也不进入剧本导入
    assert client.get("/api/sessions", headers=headers("alice")).json()["sessions"] == []


def test_r01_project_start_accepts_own_upload(client, session_dir, uploads):
    own = uploads("alice", "alice-script.txt")
    response = client.post(
        "/api/project/start",
        headers=headers("alice"),
        json={"idea": "", "script_source": "upload", "file_path": own, **MODELS},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["params"]["file_path"] == own
    state = workflow_engine.get_state(body["session_id"])
    assert state is not None and state.meta["file_path"] == own


def test_r01_import_agent_rejects_foreign_upload(uploads):
    foreign = uploads("bob", "bob-secret.txt", "bob 的剧本")
    with pytest.raises(ValueError, match="当前用户"):
        ScriptWriterAgent._resolve_uploaded_document(foreign, "alice")
    own = uploads("alice", "own.txt")
    assert ScriptWriterAgent._resolve_uploaded_document(own, "alice").endswith("own.txt")


def test_r01_import_requires_session_owner(uploads):
    own = uploads("alice", "needs-owner.txt")
    agent = ScriptWriterAgent()
    with pytest.raises(ValueError, match="会话缺少归属信息"):
        asyncio.run(agent._run_import_script({"file_path": own, **MODELS}))


def test_r22_task_events_identity_and_snapshot(client, monkeypatch, tmp_path):
    """fetch SSE 客户端依赖的鉴权契约：缺身份 401、他人 404、本人放行。

    200 + `text/event-stream` + snapshot 帧由运行期探针验证（TestClient 关闭
    长连接会等生成器结束，不适合在单测里断言流内容）。
    """
    import pipelines.storage as storage

    # 任务元数据只在 DB；执行工作根必须落在隔离 CODE_DIR 的 executions 下
    code_root = tmp_path / "code"
    monkeypatch.setattr(settings, "CODE_DIR", str(code_root))
    task = storage.create_task("standard", {}, owner="alice")
    storage.update_task(task["task_id"], status="completed")
    assert task["output_dir"].startswith(str(code_root / "work" / "executions")), "任务工作根必须落在隔离根内"
    url = f"/api/tasks/{task['task_id']}/events"

    assert client.get(url).status_code == 401
    assert client.get(url, headers=headers("bob")).status_code == 404
    # 本人鉴权通过：进入流式处理前不返回 401/404
    assert client.get(url.replace("/events", ""), headers=headers("alice")).status_code == 200
    assert client.get(url.replace("/events", ""), headers=headers("bob")).status_code == 404
