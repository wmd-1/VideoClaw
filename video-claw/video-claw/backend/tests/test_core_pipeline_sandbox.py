"""Group 15: pipeline tasks and sandbox records fully on the DB/MinIO core.

Real storage facade → publication → commit → media gateway / personal space chain;
only the paid model outputs are faked. Runs inside the existing backend container
against `videoclaw_test` and the `videoclaw-test*` buckets only.
"""
from __future__ import annotations

import asyncio
import json
import os
import types

import pytest

import pipelines.storage as storage
from config import settings
from core import artifact_publisher, media_refs
from core import object_operations as object_ops
from core.object_operations import ObjectCleanupWorker
from core.space_service import delete_saved, public_works, save_work
from db import database
from db.repositories import FileRepo, SandboxRepo, SavedRepo, TaskRepo
from db.repositories.commits import CommitParentMissing, CommitStale
from pipelines.events import publish_task_event, task_event_stream
from pipelines.runner import PIPELINE_REGISTRY, run_pipeline_task
from pipelines.utils import artifact
from storage import get_storage

OWNER = "alice"


def headers(login: str = OWNER) -> dict:
    return {"X-User-Login-Name": login}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    executions = os.path.realpath(str(tmp_path / "executions"))
    monkeypatch.setattr(artifact_publisher, "work_roots_base", lambda: executions)
    monkeypatch.setattr(object_ops, "work_roots_base", lambda: executions)
    temp = tmp_path / "temp"
    refs = tmp_path / "refs"
    temp.mkdir()
    refs.mkdir()
    monkeypatch.setattr(settings, "TEMP_DIR", str(temp))
    monkeypatch.setattr(media_refs, "_REFS_ROOT", refs)
    assert settings.STORAGE_BUCKET.startswith("videoclaw-test")
    yield types.SimpleNamespace(executions=executions, temp=temp)


def _upload(isolated, name: str, content: bytes, login: str = OWNER) -> str:
    folder = isolated.temp / "users" / media_refs.owner_key(login)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(content)
    return f"users/{media_refs.owner_key(login)}/{name}"


def _write(path: str, content: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


def _ops(resource_id: str) -> list:
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, reason, bucket, object_key FROM object_operations WHERE resource_id = %s ORDER BY created_at",
                (resource_id,),
            )
            return list(cur.fetchall())


def _media(client, public: str, login: str = OWNER, **kwargs):
    return client.get(f"{public}?user_login={login}", **kwargs)


def _run_fake(monkeypatch, behavior, params=None, owner=OWNER) -> dict:
    # 只替换付费产出：注册名沿用真实类型，个人空间按既有 domain 归类
    monkeypatch.setitem(PIPELINE_REGISTRY, "action_transfer", behavior)
    task = storage.create_task("action_transfer", dict(params or {}), owner=owner)
    asyncio.run(run_pipeline_task(task["task_id"], "action_transfer", dict(params or {})))
    return storage.load_task(task["task_id"])


async def _happy(task_id, params):
    root = storage.task_output_dir(task_id)
    img = _write(os.path.join(root, "image_01.png"), b"img-bytes")
    await storage.publish_artifact(task_id, artifact(img, "image", "image_01"))
    await storage.publish_artifact(task_id, artifact(img, "image", "image_01"))  # 重复事件
    storage.update_task(task_id, progress=50, message="half")
    clip = _write(os.path.join(root, "clip.mp4"), b"clip-bytes")
    final = _write(os.path.join(root, "final.mp4"), b"final-bytes")
    items = [artifact(img, "image", "image_01"), artifact(clip, "video", "video_01"), artifact(final, "video", "final")]
    for item in items:
        await storage.publish_artifact(task_id, item)
    output = {
        "video_path": final,
        "nested": {"frames": [{"image_path": img}]},
        "work_dir": root,
        "input_echo": params.get("image_path"),
    }
    return output, items


# ── 15.1 / 15.2 Pipeline：DB 元数据 + 发布 + 终态复用 ────────────────
def test_pipeline_runs_db_only_and_publishes_every_artifact_once(isolated, monkeypatch, client):
    canonical_input = _upload(isolated, "in.png", b"input")
    task = _run_fake(monkeypatch, _happy, {"image_path": canonical_input})
    assert task["status"] == "completed" and task["progress"] == 100
    paths = {item["name"]: item["path"] for item in task["artifacts"]}
    assert all(path.startswith("/code/result/_media/") for path in paths.values())
    assert [item["name"] for item in task["artifacts"]] == ["image_01", "video_01", "final"]  # 同 kind/name/path 去重
    rows = FileRepo().list_by_task(task["task_id"])
    assert len(rows) == 3 and all(row["storage_kind"] == "minio" for row in rows)  # 同一文件只上传一次
    assert task["output"]["video_path"] == paths["final"]
    assert task["output"]["nested"]["frames"][0]["image_path"] == paths["image_01"]
    assert task["output"]["work_dir"] == ""  # 工作目录不是可下载媒体
    assert task["output"]["input_echo"] == canonical_input  # 物化输入回显恢复为 canonical
    assert task["input"] == {"image_path": canonical_input}
    exposed = json.dumps({k: v for k, v in task.items() if k != "output_dir"})
    assert isolated.executions not in exposed and str(isolated.temp) not in exposed
    assert not os.path.exists(task["output_dir"])  # 终态后工作根清理
    assert {op["state"] for op in _ops(task["task_id"])} == {"retained"}
    assert isinstance(task["created_at"], str) and task["completed_at"]  # 时间仍是 ISO 字符串

    api = client.get(f"/api/tasks/{task['task_id']}", headers=headers()).json()
    assert api["artifacts"] == task["artifacts"] and api["status"] == "completed"
    ranged = _media(client, paths["final"], headers={"Range": "bytes=0-4"})
    assert ranged.status_code == 206 and ranged.content == b"final"
    assert _media(client, paths["final"], login="bob").status_code == 404
    works = [w for w in public_works(OWNER)["items"] if w["source"] == "task"]
    assert works and works[0]["media"][0]["url"] == paths["final"]


def test_pipeline_failure_keeps_committed_intermediates_out_of_works(isolated, monkeypatch, client):
    async def failing(task_id, params):
        part = _write(os.path.join(storage.task_output_dir(task_id), "part.mp4"), b"part")
        await storage.publish_artifact(task_id, artifact(part, "video", "video_01"))
        raise RuntimeError("model boom")

    task = _run_fake(monkeypatch, failing)
    assert task["status"] == "failed" and "model boom" in task["error"]
    part = task["artifacts"][0]["path"]
    assert part.startswith("/code/result/_media/") and _media(client, part).status_code == 200
    assert [w for w in public_works(OWNER)["items"] if w["source"] == "task"] == []
    assert not os.path.exists(task["output_dir"])


# ── 15.4 迟到写保护 / 运行中删除 / 重启 ─────────────────────────────
def test_late_callbacks_never_resurrect_or_mutate_terminal_tasks(isolated, monkeypatch, client):
    task = _run_fake(monkeypatch, _happy)
    task_id = task["task_id"]
    assert storage.update_task(task_id, progress=10)["progress"] == 100  # 终态后进度被丢弃
    late = _write(os.path.join(storage.task_output_dir(task_id), "late.png"), b"late")
    with pytest.raises(CommitStale):
        storage.append_artifact(task_id, artifact(late, "image", "late"))
    late_ops = [op for op in _ops(task_id) if op["state"] != "retained"]
    assert late_ops and all(op["state"] == "done" for op in late_ops)  # 迟到发布已补偿
    assert all(get_storage().stat(op["object_key"], bucket=op["bucket"]) is None for op in late_ops)

    assert client.delete(f"/api/tasks/{task_id}", headers=headers()).status_code == 200
    assert storage.update_task(task_id, progress=99) == {}
    with pytest.raises((FileNotFoundError, CommitParentMissing)):
        storage.append_artifact(task_id, artifact(late, "image", "late"))
    assert TaskRepo().get(task_id) is None  # 删除后回调不复活父行
    final = task["output"]["video_path"]
    assert _media(client, final).status_code == 404
    counts = ObjectCleanupWorker(boot_id="other").run_once()
    assert counts["deleted"] >= 3
    assert all(state == "done" for state in {op["state"] for op in _ops(task_id)})


def test_running_task_delete_conflicts_and_restart_interrupts(isolated, client):
    task = storage.create_task("standard", {}, owner=OWNER)
    storage.mark_running(task["task_id"])
    assert client.delete(f"/api/tasks/{task['task_id']}", headers=headers()).status_code == 409
    assert storage.load_task(task["task_id"])["status"] == "running"
    assert TaskRepo().sweep_stale_running("another-boot") == 1
    assert storage.load_task(task["task_id"])["status"] == "interrupted"  # 不自动续跑
    retry = storage.create_task("standard", {}, owner=OWNER)
    assert retry["task_id"] != task["task_id"]  # 显式重试是新任务
    assert client.delete(f"/api/tasks/{task['task_id']}", headers=headers()).status_code == 200


def test_task_events_published_from_worker_threads_reach_subscribers():
    async def main():
        stream = task_event_stream("evt_task", initial_event={"type": "snapshot", "status": "running"})
        first = await stream.__anext__()
        await asyncio.to_thread(publish_task_event, "evt_task", {"type": "completed", "status": "completed"})
        second = await asyncio.wait_for(stream.__anext__(), 2)
        await stream.aclose()
        return first, second

    first, second = asyncio.run(main())
    assert '"snapshot"' in first and '"completed"' in second


# ── 15.3 Sandbox：记录/文件/路径同事务，暂存音频受管 ───────────────────
def test_sandbox_video_record_files_and_staged_audio(isolated, monkeypatch, client):
    from models import video_client

    audio = _upload(isolated, "voice.mp3", b"audio-bytes")
    captured = {}

    def fake_generate_video(self, **kwargs):
        captured.update(kwargs)
        _write(kwargs["save_path"], b"sandbox-video")
        return {"video_url": "http://example.invalid/v.mp4", "local_path": kwargs["save_path"]}

    monkeypatch.setattr(video_client.VideoClient, "generate_video", fake_generate_video)
    body = client.post("/api/sandbox/video", headers=headers(),
                       json={"model": "m", "prompt": "p", "audio_url": audio}).json()
    assert body["success"] is True, body
    assert captured["audio_reference_url"].startswith("http")  # 受管暂存的预签名，而非裸 /code 地址
    assert "/code/" not in captured["audio_reference_url"]
    record_id = body["record_id"]
    record = SandboxRepo().get(record_id)
    assert record["input_json"]["audio_url"] == audio  # 记录只存 canonical
    video_path = body["video_path"]
    assert video_path.startswith("/code/result/_media/") and record["output_json"]["video_path"] == video_path
    assert record["output_json"]["video"]["local_path"] == video_path  # 输出回显的工作路径同样映射
    assert _media(client, video_path).content == b"sandbox-video"
    file_row = FileRepo().get_by_public_path(video_path)
    assert file_row["record_id"] == record_id and file_row["owner"] == OWNER
    staging = [op for op in _ops(record_id) if op["reason"] == "staging"]
    assert staging and all(op["state"] == "delete_pending" for op in staging)  # 请求结束即交清理器
    history = client.get("/api/sandbox/history", headers=headers()).json()["records"]
    assert [item["id"] for item in history] == [record_id]
    detail = client.get(f"/api/sandbox/history/{record_id}", headers=headers()).json()
    assert detail["success"] and detail["record"]["output"]["video_path"] == video_path
    assert client.get(f"/api/sandbox/history/{record_id}", headers=headers("bob")).json()["success"] is False

    assert client.delete(f"/api/sandbox/history/{record_id}", headers=headers("bob")).json()["success"] is False
    assert client.delete(f"/api/sandbox/history/{record_id}", headers=headers()).json()["success"] is True
    assert SandboxRepo().get(record_id) is None and _media(client, video_path).status_code == 404
    ObjectCleanupWorker(boot_id="other").run_once()
    assert all(op["state"] == "done" for op in _ops(record_id))
    assert all(get_storage().stat(op["object_key"], bucket=op["bucket"]) is None for op in _ops(record_id))


def test_sandbox_failure_writes_no_record_and_text_tools_record_without_files(isolated, monkeypatch, client):
    from models import llm_client, video_client

    def failing_video(self, **kwargs):
        _write(kwargs["save_path"], b"half")
        raise RuntimeError("provider down")

    monkeypatch.setattr(video_client.VideoClient, "generate_video", failing_video)
    body = client.post("/api/sandbox/video", headers=headers(), json={"model": "m", "prompt": "p"}).json()
    assert body["success"] is False and "provider down" in body["error"]
    assert SandboxRepo().list(OWNER) == []
    with database.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM artifact_files")
            assert cur.fetchone()["n"] == 0
    assert not os.listdir(isolated.executions) or not any(
        files for _, _, files in os.walk(isolated.executions)
    )  # 失败不留工作文件

    monkeypatch.setattr(llm_client.LLM, "query", lambda self, prompt, **kwargs: "hello")
    body = client.post("/api/sandbox/llm", headers=headers(), json={"model": "m", "prompt": "hi"}).json()
    assert body["success"] is True
    record = SandboxRepo().get(body["record_id"])
    assert record["output_json"] == {"response": "hello"} and record["files_json"] == []


def test_sandbox_t2i_publishes_images_from_the_work_root(isolated, monkeypatch, client):
    from models import image_client

    def fake_generate_image(self, prompt, image_paths=None, model="", save_dir=None, **kwargs):
        return [_write(os.path.join(save_dir, "out_1.png"), b"png-1")]

    monkeypatch.setattr(image_client.ImageClient, "generate_image", fake_generate_image)
    body = client.post("/api/sandbox/t2i", headers=headers(), json={"model": "m", "prompt": "cat"}).json()
    assert body["success"] is True, body
    [image] = body["result"]
    assert image.startswith("/code/result/_media/") and _media(client, image).content == b"png-1"
    assert SandboxRepo().get(body["record_id"])["files_json"] == [image]


# ── 15.5 收藏：独立副本，源删除不影响；取消收藏走清理日志 ─────────────────
def test_saved_copy_survives_source_delete_and_unsave_queues_cleanup(isolated, monkeypatch, client):
    task = _run_fake(monkeypatch, _happy)
    work = next(w for w in public_works(OWNER)["items"] if w["source"] == "task")
    saved = save_work(OWNER, work["work_id"])
    row = SavedRepo().get(saved["saved_id"])
    assert row["bucket"] == settings.STORAGE_SAVED_BUCKET
    assert [op["state"] for op in _ops(saved["saved_id"])] == ["retained"]
    url = f"/api/space/saved/{saved['saved_id']}/media?user_login={OWNER}"
    assert client.get(url).content == b"final-bytes"

    assert storage.delete_task(task["task_id"], OWNER)
    ObjectCleanupWorker(boot_id="other").run_once()
    assert client.get(url).content == b"final-bytes"  # 源删除不影响独立副本

    assert delete_saved(OWNER, saved["saved_id"])
    assert client.get(url).status_code == 404
    assert [op["state"] for op in _ops(saved["saved_id"])] == ["delete_pending"]
    ObjectCleanupWorker(boot_id="other").run_once()
    assert [op["state"] for op in _ops(saved["saved_id"])] == ["done"]
    assert get_storage().stat(row["object_key"], bucket=row["bucket"]) is None
