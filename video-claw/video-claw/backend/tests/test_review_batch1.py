"""第六轮报告局部批次回归：R09/R10/R12/R14/R16。

与 test_user_space.py 相同，只允许在既有后端容器内执行：
  docker exec -i -e PYTHONPATH=/app -w /app video-claw-backend \\
    /app/.venv/bin/python -m pytest /tmp/vc_tests -q
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import uuid

import pytest

from api.dependencies import workflow_engine
from config import settings
from core.identity import CurrentUser, owner_key
from core.media_refs import to_public
from core.space_service import public_works, save_work
from db.repositories import FileRepo, SavedRepo, TaskRepo
from storage import get_storage


def headers(login: str) -> dict[str, str]:
    return {"X-User-Login-Name": login}


def make_file_row(session_id: str, path: str, owner: str = "alice", stage: str = "video_generation") -> dict:
    public = to_public(path)
    row = {
        "file_id": hashlib.sha256((session_id + public).encode()).hexdigest()[:32],
        "owner": owner,
        "session_id": session_id,
        "stage": stage,
        "kind": "video" if path.endswith(".mp4") else "file",
        "storage_kind": "local",
        "object_key": public.lstrip("/"),
        "public_path": public,
        "local_path": path,
        "media_type": "video/mp4" if path.endswith(".mp4") else "application/octet-stream",
        "size_bytes": os.path.getsize(path),
    }
    FileRepo().upsert_many([row])
    return row


def write_bytes(path: str, content: bytes) -> str:
    with open(path, "wb") as handle:
        handle.write(content)
    return path


class CreatedResource:
    """登记本轮测试创建的对象/记录，并在 finally 中强制清理。

    登记发生在资源创建「之前或紧随其后、任何断言之前」，因此断言失败不会
    把清理入口留空；清理失败不静默吞掉，而是带 bucket/key 上报。
    """

    def __init__(self) -> None:
        self._objects: list[tuple[str, str]] = []
        self._records: list[tuple[str, str]] = []
        self._paths: list[str] = []
        self.errors: list[str] = []

    def object(self, bucket: str, key: str) -> None:
        if bucket and key:
            self._objects.append((bucket, key))

    def record(self, saved_id: str, owner: str) -> None:
        if saved_id:
            self._records.append((saved_id, owner))

    def path(self, path: str) -> None:
        if path:
            self._paths.append(path)

    @property
    def objects(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._objects)

    def cleanup(self) -> list[str]:
        storage = get_storage()
        object_errors: list[str] = []
        for bucket, key in self._objects:
            try:
                storage.delete(key, bucket=bucket)
                if storage.stat(key, bucket=bucket) is not None:
                    object_errors.append(f"对象仍存在: bucket={bucket} key={key}")
            except Exception as exc:  # 清理失败必须上报，不能静默
                object_errors.append(f"对象删除异常: bucket={bucket} key={key}: {exc}")
        self.errors.extend(object_errors)
        for saved_id, owner in self._records:
            if object_errors:
                # 对象没清掉时保留索引记录，后续还能定位到遗留对象
                self.errors.append(
                    f"对象未清理，保留索引记录: saved_id={saved_id} owner={owner}"
                )
                continue
            try:
                SavedRepo().delete(saved_id, owner)
            except Exception as exc:
                self.errors.append(f"记录删除异常: saved_id={saved_id} owner={owner}: {exc}")
        for path in self._paths:
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError as exc:
                self.errors.append(f"本地文件删除失败: {path}: {exc}")
        return self.errors


def track_saved_copy(monkeypatch, created: "CreatedResource") -> None:
    """在真实复制/插入入口「之前」登记目标 bucket/key 与记录 ID。

    `save_work` 先复制对象再插入记录，最后才回查并返回；如果只等它返回再登记，
    复制后、返回前的异常窗口就无法清理。因此包装底层入口先登记再调用原实现。
    """
    import core.space_service as space_module

    real_copy = space_module._copy_source
    real_commit = space_module.commit_saved_item

    def copy_first(source, target_bucket, target_key):
        created.object(str(target_bucket), str(target_key))
        return real_copy(source, target_bucket, target_key)

    def commit_first(*, item, operation_id):
        # 收藏行与副本 retained 同事务提交；登记先于提交
        created.record(str(item["item_id"]), str(item.get("owner") or ""))
        return real_commit(item=item, operation_id=operation_id)

    monkeypatch.setattr(space_module, "_copy_source", copy_first)
    monkeypatch.setattr(space_module, "commit_saved_item", commit_first)


def assert_cleaned(resource: CreatedResource) -> None:
    """执行清理；失败时附到当前异常备注，无异常则直接失败。"""
    errors = resource.cleanup()
    if not errors:
        return
    message = "测试清理失败：\n" + "\n".join(errors)
    active = sys.exc_info()[1]
    if active is not None and hasattr(active, "add_note"):
        active.add_note(message)
        return
    raise AssertionError(message)


# ─────────────── R09：字典媒体选择不能退化成 set ───────────────


def test_r09_dict_media_selection_keeps_entry_ids(client, session_dir, result_file):
    import core.media_refs as refs

    workflow_engine.create_session("r09_sid", {}, owner="alice")
    second_path = write_bytes(os.path.join(os.path.dirname(result_file), "second.bin"), b"second")
    first = make_file_row("r09_sid", result_file)
    second = make_file_row("r09_sid", second_path)
    user = CurrentUser(login="alice", owner_key=owner_key("alice"))

    payload = {
        "selected_images": {"img_a": result_file, "img_b": second_path},
        "selected_clips": {"clip_1": {"selected": result_file}},
    }
    normalized = refs.canonicalize_media_payload(payload, user, "r09_sid")

    selected = normalized["selected_images"]
    assert isinstance(selected, dict) and not isinstance(selected, set)
    assert set(selected) == {"img_a", "img_b"}
    assert selected["img_a"] == first["public_path"]
    assert selected["img_b"] == second["public_path"]
    # 嵌套 {id: {selected: ...}} 同样保留条目 ID
    assert normalized["selected_clips"] == {"clip_1": {"selected": first["public_path"]}}

    # 真实干预入口：PATCH 归一化后仍是字典，条目 ID 不丢
    response = client.patch(
        "/api/project/r09_sid/artifact/video_generation",
        headers=headers("alice"),
        json={"selected_images": {"img_a": result_file, "img_b": second_path}},
    )
    assert response.status_code == 200
    patched = response.json()["artifact"]["selected_images"]
    assert isinstance(patched, dict)
    assert set(patched) == {"img_a", "img_b"}
    assert patched["img_a"] == first["public_path"]
    assert patched["img_b"] == second["public_path"]


# ─────────────── R10：MinIO 物化结果保留 trusted ───────────────


def test_r10_minio_materialize_keeps_trusted_for_copy(session_dir, result_file):
    import core.media_refs as refs
    from api.media_paths import guard_media_input
    from pipelines.utils import copy_input_file

    workflow_engine.create_session("r10_sid", {}, owner="alice")
    public = to_public(result_file)
    content = open(result_file, "rb").read()
    object_key = f"tests/r10/{uuid.uuid4().hex}.bin"
    storage = get_storage()
    user = CurrentUser(login="alice", owner_key=owner_key("alice"))
    target = refs._ref_target(public, user.owner_key)
    if target.exists():
        target.unlink()
    created = CreatedResource()
    # 上传前先登记本轮唯一对象位置与 _refs 缓存路径，异常窗口内也能清理
    created.object(settings.STORAGE_BUCKET, object_key)
    created.path(str(target))
    try:
        storage.upload(result_file, object_key, "application/octet-stream")
        FileRepo().upsert_many([{
            "file_id": "r10-file", "owner": "alice", "session_id": "r10_sid",
            "stage": "video_generation", "kind": "file", "storage_kind": "minio",
            "bucket": settings.STORAGE_BUCKET, "object_key": object_key, "public_path": public,
            "local_path": None, "media_type": "application/octet-stream",
        }])
        # MinIO-only：本地文件已删除，必须走 ensure_local 的真实下载分支
        os.remove(result_file)
        assert not os.path.isfile(result_file)

        materialized = refs.materialize_media_payload({"image_path": public}, user)["image_path"]
        assert isinstance(materialized, refs.ResolvedMedia) and materialized.trusted
        assert os.path.realpath(str(materialized)).startswith(os.path.realpath(str(refs._REFS_ROOT)))
        assert open(str(materialized), "rb").read() == content

        # 真实消费链：Pipeline 复制入口接受可信 _refs 缓存
        out_dir = tempfile.mkdtemp(dir=os.path.dirname(result_file))
        try:
            copied = copy_input_file(materialized, out_dir, "input_image")
            assert os.path.isfile(copied) and open(copied, "rb").read() == content
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

        # 原始字符串（伪造/他人 _refs）仍在白名单外，允许根未被扩大
        with pytest.raises(ValueError):
            guard_media_input(str(materialized), "input_path")
    finally:
        assert_cleaned(created)


# ─────────────── R12：sandbox 视频接口的图片保持可选 ───────────────


def test_r12_sandbox_video_optional_image(client, monkeypatch, tmp_path, session_dir, result_file):
    from core import artifact_publisher
    from models import video_client

    # 执行工作根落在隔离目录；记录与文件索引只在测试库
    executions = os.path.realpath(str(tmp_path / "executions"))
    monkeypatch.setattr(artifact_publisher, "work_roots_base", lambda: executions)
    captured: dict = {}

    def fake_generate_video(self, **kwargs):
        captured.clear()
        captured.update(kwargs)
        write_bytes(kwargs["save_path"], b"\x00")
        return {"video_url": "http://example.invalid/v.mp4"}

    monkeypatch.setattr(video_client.VideoClient, "generate_video", fake_generate_video)
    workflow_engine.create_session("r12_sid", {}, owner="alice")
    reference_row = make_file_row("r12_sid", result_file)

    # 纯文生视频：image 缺省
    body = client.post(
        "/api/sandbox/video", headers=headers("alice"), json={"model": "m", "prompt": "p"}
    ).json()
    assert body["success"] is True, body
    assert captured["image_path"] == ""

    # 仅参考视频
    body = client.post(
        "/api/sandbox/video",
        headers=headers("alice"),
        json={"model": "m", "prompt": "p", "reference_videos": [reference_row["public_path"]]},
    ).json()
    assert body["success"] is True, body
    assert captured["image_path"] == ""
    assert len(captured["reference_video_paths"]) == 1

    # 仍支持图片参考
    body = client.post(
        "/api/sandbox/video",
        headers=headers("alice"),
        json={"model": "m", "prompt": "p", "image": reference_row["public_path"]},
    ).json()
    assert body["success"] is True, body
    assert captured["image_path"]


# ─────────────── R14：导入 action 标注进入分镜单元 ───────────────


def test_r14_imported_action_lines_become_storyboard_units():
    from core.agents.storyboard_agent import StoryboardAgent
    from core.script_import import annotate_lines

    text = "小陈走进办公室，把材料放在桌上。\n小陈：这份材料我先放这儿。"
    lines = text.split("\n")
    annotations = annotate_lines(lines, [], (None, None))
    assert [item["type"] for item in annotations] == ["action", "dialogue"]

    _, units = StoryboardAgent._annotate_episode_script(
        text,
        [{"name": "小陈"}],
        [],
        annotations,
        [],
        line_offset=0,
        baseline_line_count=len(lines),
    )
    assert [unit["text"] for unit in units] == lines
    assert [unit["is_dialogue"] for unit in units] == [False, True]
    assert units[0]["unit_id"] == "U001"


# ─────────────── R16：作品/收藏取最终成片 ───────────────


def _make_final_task_work(session_id: str, task_id: str, result_file: str) -> tuple[str, dict]:
    """构造 completed 任务：输入视频在前、final 在后。"""
    workflow_engine.create_session(session_id, {}, owner="alice")
    final_path = write_bytes(
        os.path.join(os.path.dirname(result_file), f"{task_id}-final.mp4"), b"final-bytes"
    )
    final_row = make_file_row(session_id, final_path)
    make_file_row(session_id, result_file)
    TaskRepo().upsert({
        "task_id": task_id,
        "owner": "alice",
        "pipeline": "action_transfer",
        "status": "completed",
        "output": {"video_path": final_path},
        "artifacts": [
            {"kind": "video", "name": "input_video", "path": result_file},
            {"kind": "video", "name": "final", "path": final_path},
        ],
    })
    works = [item for item in public_works("alice")["items"] if item["source"] == "task"]
    assert len(works) == 1
    return works[0]["work_id"], final_row


def _save_final_work(work_id: str, *, fail_after_save: bool = False):
    """真实的保存链；对象/记录由 track_saved_copy 在复制、插入前登记。"""
    saved = save_work("alice", work_id)
    if fail_after_save:
        raise AssertionError("故意失败：模拟保存返回后、断言前的异常")
    return saved, SavedRepo().by_work("alice", work_id)


def test_r16_task_work_and_saved_copy_use_final_artifact(session_dir, result_file, monkeypatch):
    work_id, final_row = _make_final_task_work("r16_sid", "r16_task", result_file)
    task_works = [item for item in public_works("alice")["items"] if item["source"] == "task"]
    assert task_works[0]["media"][0]["url"] == final_row["public_path"]

    created = CreatedResource()
    track_saved_copy(monkeypatch, created)
    try:
        saved, row = _save_final_work(work_id)
        assert row and row["item_id"] == saved["saved_id"]
        # 收藏副本必须落在隔离测试桶，不能写业务桶 videoclaw-saved
        assert row["bucket"] == settings.STORAGE_SAVED_BUCKET
        assert settings.STORAGE_SAVED_BUCKET != "videoclaw-saved"
        # 登记发生在复制前，而不是等到 save_work 返回
        assert created.objects and created.objects[0] == (
            str(row["bucket"]), str(row["object_key"])
        )
        downloaded = os.path.join(tempfile.mkdtemp(dir=os.path.dirname(result_file)), "saved.bin")
        try:
            get_storage().download(str(row["object_key"]), downloaded, bucket=str(row["bucket"]))
            assert open(downloaded, "rb").read() == b"final-bytes"
        finally:
            shutil.rmtree(os.path.dirname(downloaded), ignore_errors=True)
        assert saved["media"][0]["url"] == f"/api/space/saved/{saved['saved_id']}/media"
    finally:
        assert_cleaned(created)


def test_r16_cleanup_runs_when_failing_after_create(session_dir, result_file, monkeypatch):
    work_id, _ = _make_final_task_work("r16c_sid", "r16c_task", result_file)
    created = CreatedResource()
    track_saved_copy(monkeypatch, created)

    # 对象创建后主动抛异常：清理不能依赖后续断言成功
    with pytest.raises(AssertionError, match="故意失败"):
        try:
            _save_final_work(work_id, fail_after_save=True)
        finally:
            assert_cleaned(created)

    assert created.errors == [], created.errors
    assert created.objects, "应已登记收藏对象"
    bucket, key = created.objects[0]
    assert bucket == settings.STORAGE_SAVED_BUCKET
    assert get_storage().stat(key, bucket=bucket) is None
    assert SavedRepo().by_work("alice", work_id) is None


def test_r16_cleanup_when_copy_fails_before_save_returns(session_dir, result_file, monkeypatch):
    """真实复制失败、save_work 尚未返回：清理器必须已知道目标对象。"""
    import core.space_service as space_module

    work_id, _ = _make_final_task_work("r16d_sid", "r16d_task", result_file)
    created = CreatedResource()
    # 登记由包装器完成，且发生在真实 _copy_source 之前
    track_saved_copy(monkeypatch, created)

    class FailingStorage:
        """只让对象写入失败；清理器用的是测试模块自己的真实存储实例。"""

        def upload(self, *args, **kwargs):
            raise RuntimeError("copy boom")

        def copy(self, *args, **kwargs):
            raise RuntimeError("copy boom")

        def delete(self, key, bucket=None):
            return None

    monkeypatch.setattr(space_module, "get_storage", lambda: FailingStorage())
    with pytest.raises(RuntimeError, match="copy boom"):
        try:
            save_work("alice", work_id)
        finally:
            assert_cleaned(created)

    assert created.errors == [], created.errors
    assert created.objects, "登记必须早于真实复制，否则这个异常窗口无法清理"
    bucket, key = created.objects[0]
    assert bucket == settings.STORAGE_SAVED_BUCKET
    assert get_storage().stat(key, bucket=bucket) is None
    assert SavedRepo().by_work("alice", work_id) is None


def test_r16_cleanup_when_lookup_fails_after_save(session_dir, result_file, monkeypatch):
    """保存返回后定位查询失败：清理不依赖查询，对象与记录仍被清掉。"""
    work_id, _ = _make_final_task_work("r16e_sid", "r16e_task", result_file)
    created = CreatedResource()
    track_saved_copy(monkeypatch, created)

    def broken_lookup(*args, **kwargs):
        raise RuntimeError("lookup boom")

    try:
        # 保存本身也在保护范围内：复制/插入/组装任一环失败都不会绕过清理，
        # 且保存失败不会被当作预期的 lookup 异常。
        save_work("alice", work_id)
        monkeypatch.setattr(SavedRepo, "by_work", broken_lookup)
        with pytest.raises(RuntimeError, match="lookup boom"):
            SavedRepo().by_work("alice", work_id)
    finally:
        assert_cleaned(created)

    assert created.errors == [], created.errors
    assert created.objects, "对象应在复制前登记"
    bucket, key = created.objects[0]
    assert get_storage().stat(key, bucket=bucket) is None
    assert SavedRepo().count("alice") == 0


def test_created_resource_reports_cleanup_failure(monkeypatch):
    # 清理失败必须显式上报并保留定位信息，不能 except ... pass 静默吞掉
    class BrokenStorage:
        def delete(self, key, bucket=None):
            raise RuntimeError("delete boom")

        def stat(self, key, bucket=None):
            return {"size": 1}

    monkeypatch.setattr(sys.modules[__name__], "get_storage", lambda: BrokenStorage())
    removed_records: list[str] = []
    monkeypatch.setattr(
        SavedRepo, "delete",
        lambda self, saved_id, owner: removed_records.append(saved_id) or True,
    )
    resource = CreatedResource()
    resource.object("videoclaw-test-saved", "owner/key.mp4")
    resource.record("sv_probe", "alice")

    with pytest.raises(AssertionError) as failure:
        assert_cleaned(resource)
    message = str(failure.value)
    assert "测试清理失败" in message
    assert "delete boom" in message
    assert "videoclaw-test-saved" in message and "owner/key.mp4" in message
    # 对象没清掉时不得删索引记录，保留可追踪信息
    assert removed_records == []
    assert "保留索引记录" in message and "sv_probe" in message

    # 删除不报错但对象仍在，也要判为清理失败
    class SilentStorage:
        def delete(self, key, bucket=None):
            return None

        def stat(self, key, bucket=None):
            return {"size": 1}

    monkeypatch.setattr(sys.modules[__name__], "get_storage", lambda: SilentStorage())
    leftover = CreatedResource()
    leftover.object("videoclaw-test-saved", "owner/left.mp4")
    errors = leftover.cleanup()
    assert errors and "对象仍存在" in errors[0]


def test_r16_intermediate_clips_without_final_are_not_works(session_dir, result_file):
    workflow_engine.create_session("r16b_sid", {}, owner="alice")
    second_path = write_bytes(os.path.join(os.path.dirname(result_file), "video_02.mp4"), b"clip-2")
    make_file_row("r16b_sid", result_file)
    make_file_row("r16b_sid", second_path)
    TaskRepo().upsert({
        "task_id": "r16b_task",
        "owner": "alice",
        "pipeline": "standard",
        "status": "completed",
        "output": {},
        "artifacts": [
            {"kind": "video", "name": "video_01", "path": result_file},
            {"kind": "video", "name": "video_02", "path": second_path},
        ],
    })

    # 没有 final、也没有权威输出：不展示、不收藏
    assert public_works("alice")["items"] == []
    with pytest.raises(KeyError):
        save_work("alice", "task:r16b_task:final")
