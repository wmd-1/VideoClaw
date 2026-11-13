# -*- coding: utf-8 -*-
"""
核心编排器 / 工作流引擎
管理六阶段状态机，协调各智能体执行，支持用户在任意阶段介入
"""

import asyncio
import copy
import logging
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

from config import settings
from core import media_refs, script_revision, workflow_store
from core.artifact_publisher import build_file_rows, new_execution_id
from core.asset_kind import asset_kind_for
from core.grounding_cache import grounding_cache
from core.observability import call_context
from core.language import language_from_meta, resolve_language
from core.persistence_service import PersistenceService
from core.reference_skip import build_reference_skip, reference_skip_marker
from db.repositories.commits import (
    CommitConflict,
    CommitParentMissing,
    CommitStale,
    create_session_row,
    delete_session_cascade,
    list_session_summaries,
    load_committed,
    session_ids,
    sessions_with_running,
)
from core.agents import (
    CharacterDesignerAgent,
    PromptRewriteAgent,
    ReferenceGeneratorAgent,
    ScriptWriterAgent,
    StoryboardAgent,
    VideoDirectorAgent,
    VideoEditorAgent,
)

logger = logging.getLogger(__name__)

# 上传目标标识白名单（openspec: artifact-upload-path-safety）：
# session_id / item_id 必须是字母、数字、下划线、连字符。
UPLOAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# base 可能带 char_/set_ 前缀，长度上限放宽到 80，字符集不变。
UPLOAD_BASE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _is_valid_upload_identifier(value: Any, pattern: re.Pattern = UPLOAD_ID_PATTERN) -> bool:
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def validate_upload_identifier(value: Any, field: str, pattern: re.Pattern = UPLOAD_ID_PATTERN) -> str:
    """校验上传目标标识；非法即抛带字段名的 ValueError（路由映射 400）。"""
    if not _is_valid_upload_identifier(value, pattern):
        raise ValueError(f"{field} 标识格式非法（仅允许字母、数字、下划线、连字符，长度受限）")
    return value


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


class WorkflowStage(str, Enum):
    """工作流阶段"""
    INIT = "init"
    SCRIPT_GENERATION = "script_generation"
    CHARACTER_DESIGN = "character_design"
    STORYBOARD = "storyboard"
    REFERENCE_GENERATION = "reference_generation"
    PROMPT_REWRITE = "prompt_rewrite"
    VIDEO_GENERATION = "video_generation"
    POST_PRODUCTION = "post_production"
    COMPLETED = "completed"


def get_stage_order() -> List[WorkflowStage]:
    """按配置组装阶段顺序。

    h3_rewrite.enable=true 时在「参考图」与「视频」之间注入「提示词改写」（七阶段）；
    默认禁用时保持原六阶段（阶段不注入流程，而非注册后跳过）。
    """
    from config import Config  # 惰性导入，避免循环依赖

    order = [
        WorkflowStage.SCRIPT_GENERATION,
        WorkflowStage.CHARACTER_DESIGN,
        WorkflowStage.STORYBOARD,
        WorkflowStage.REFERENCE_GENERATION,
        WorkflowStage.VIDEO_GENERATION,
        WorkflowStage.POST_PRODUCTION,
    ]
    if Config.H3_REWRITE_ENABLED:
        order.insert(order.index(WorkflowStage.REFERENCE_GENERATION) + 1, WorkflowStage.PROMPT_REWRITE)
    return order


# 固定全序（含全部可选阶段）：_get_next_stage 在 current 因开关关闭而从启用序列
# 消失时（如会话停留在 prompt_rewrite 停点后 h3_rewrite.enable 被关闭），按此全序
# 回退到下一个仍启用的阶段，保证会话恢复/继续不死锁。
_FULL_STAGE_ORDER = [
    WorkflowStage.SCRIPT_GENERATION,
    WorkflowStage.CHARACTER_DESIGN,
    WorkflowStage.STORYBOARD,
    WorkflowStage.REFERENCE_GENERATION,
    WorkflowStage.PROMPT_REWRITE,
    WorkflowStage.VIDEO_GENERATION,
    WorkflowStage.POST_PRODUCTION,
]

SESSION_META_KEYS = (
    "idea",
    "user_textbox_input",
    "style",
    "video_ratio",
    "video_resolution",
    "expand_idea",
    "llm_model",
    "vlm_model",
    "image_t2i_model",
    "image_it2i_model",
    "video_model",
    "video_first_frame_model",
    "video_start_end_model",
    "video_reference_model",
    "video_generation_mode",
    "video_style",
    "enable_concurrency",
    "web_search",
    "episodes",
)


def _normalize_meta_value(value: Any) -> Any:
    if isinstance(value, str):
        lower = value.lower()
        if lower == "true":
            return True
        if lower == "false":
            return False
    return value


def _episode_body_signature(episodes: Any) -> List[tuple]:
    """集身份 + 顺序 + 正文；标题等非正文变化不计入。"""
    signature: List[tuple] = []
    for episode in episodes if isinstance(episodes, list) else []:
        if not isinstance(episode, dict):
            continue
        identity = str(episode.get("episode_number") or episode.get("act_number") or "")
        signature.append((identity, str(episode.get("content") or "")))
    return signature


def _has_import_coordinates(artifact: Dict[str, Any]) -> bool:
    """是否带有导入路径派生的行坐标（标注或原始全文）。"""
    meta = artifact.get("meta") if isinstance(artifact.get("meta"), dict) else {}
    return bool(artifact.get("line_annotations")) or bool(meta.get("original_text"))


def _assign_episode_line_ranges(episodes: List[dict]) -> List[str]:
    """按传入顺序重算每集的全局行区间（与导入切分口径一致），返回全文行列表。"""
    lines: List[str] = []
    for episode in episodes:
        if not isinstance(episode, dict):
            continue
        content_lines = str(episode.get("content") or "").split("\n")
        start = len(lines)
        lines.extend(content_lines)
        episode["line_start"] = start
        episode["line_end"] = max(start, len(lines) - 1)
    return lines


def _rebuild_import_annotations(artifact: Dict[str, Any], lines: List[str]) -> None:
    """正文真变化后重建导入派生的行坐标（R15）。

    - 行标注复用导入同口径的确定性流程（含角色设定区间），避免设定行被当成对白；
    - scene_spans 按新正文的幕/场标题 + 现有场景名重新匹配，而不是直接清空；
    - meta.original_text 同步为新正文，供分镜的行数上限判断使用。
    """
    from core import script_import

    if not lines:
        return
    candidates = script_import.detect_candidates(lines)
    annotations = script_import.deterministic_annotations(lines)
    artifact["line_annotations"] = annotations
    artifact["scene_spans"] = _rebuild_scene_spans(artifact, lines, candidates, annotations)
    # 正文已变，旧的质检行号与时长建议不再可信：保留结论但标记过期
    script_import.mark_import_review_stale(artifact)
    meta = artifact.get("meta")
    if not isinstance(meta, dict):
        meta = {}
        artifact["meta"] = meta
    meta["original_text"] = "\n".join(lines)


def _description_segment_lines(
    annotation_by_line: Dict[int, dict], start: int, end: int
) -> List[int]:
    """从 scene_desc 标记行开始的描述段行号；遇对白/旁白/标题等边界立即停止。

    导入格式允许【画面描述】单独成行、地点写在下一行的正文里；不能只看标记行，
    也不能把任意对白当作地点来源。
    """
    marker = annotation_by_line.get(start) or {}
    if str(marker.get("type") or "") != "scene_desc":
        return []
    boundary = {"dialogue", "narration", "heading", "meta", "cast_block", "subtitle", "end_card", "placeholder"}
    segment = [start]
    for line_no in range(start + 1, end + 1):
        annotation = annotation_by_line.get(line_no)
        if annotation is None:
            continue  # 空行不终结描述段
        if str(annotation.get("type") or "") in boundary:
            break
        segment.append(line_no)
    return segment


def _rebuild_scene_spans(
    artifact: Dict[str, Any], lines: List[str], candidates: List[dict], annotations: List[dict]
) -> List[dict]:
    """用现有场景名/别名重建场景区间：标题优先，标题不命中时只看本幕/场的 scene_desc。

    导入格式里地点可能只出现在【画面描述】行（标题写作“开场/收尾”）；但不得把
    对白里提到的地点当作切场信号。
    """
    from core import script_import

    settings = artifact.get("settings")
    pairs: List[tuple] = []
    for item in settings if isinstance(settings, list) else []:
        if not isinstance(item, dict):
            continue
        canonical = str(item.get("name") or "").strip()
        if not canonical:
            continue
        pairs.append((canonical, canonical))
        for alias in item.get("aliases") or []:
            text = str(alias).strip()
            if text:
                pairs.append((text, canonical))
    # 长名优先，避免“办公室”先把“贷审会会议室”吃掉
    pairs.sort(key=lambda pair: len(str(pair[0])), reverse=True)

    def match_setting(text: str) -> str:
        for alias, canonical in pairs:
            if alias and alias in text:
                return canonical
        return ""

    headings = [
        item for item in candidates
        if isinstance(item, dict) and item.get("level") in (script_import.LEVEL_ACT, script_import.LEVEL_SCENE)
    ]
    annotation_by_line = {
        int(item["line"]): item
        for item in annotations
        if isinstance(item, dict) and "line" in item
    }
    mapping: Dict[int, str] = {}
    for index, heading in enumerate(headings):
        start = int(heading["line"])
        end = (
            int(headings[index + 1]["line"]) - 1
            if index + 1 < len(headings)
            else len(lines) - 1
        )
        setting = match_setting(str(heading.get("title") or ""))
        if not setting:
            # 标题不写地点：只从本幕/场内的显式描述段取地点
            for line_no in range(start + 1, end + 1):
                segment = _description_segment_lines(annotation_by_line, line_no, end)
                for seg_line in segment:
                    if seg_line >= len(lines):
                        continue
                    setting = match_setting(lines[seg_line])
                    if setting:
                        break
                if setting:
                    break
        if setting:
            mapping[start] = setting
    return script_import.build_scene_spans(lines, candidates, mapping)


class WorkflowState:
    """工作流状态"""

    # 阶段状态说明：
    # - pending: 阶段还没有产物，也没有在运行
    # - running: 阶段正在执行
    # - waiting: 阶段已产出内容，但需要用户介入（如选择角色、选择图片等）
    # - completed: 阶段已完成，可进入下一阶段
    # - stopped: 阶段被用户手动停止
    # - error: 阶段执行中遇到错误

    def __init__(self, session_id: str):
        self.session_id = session_id
        # owner 是归属判据，不是认证凭据；旧会话按 users.legacy_owner 归一。
        self.owner: str = ""
        self.current_stage: WorkflowStage = WorkflowStage.INIT
        self.status: Dict[str, str] = {
            stage.value: "pending" for stage in WorkflowStage if stage != WorkflowStage.INIT and stage != WorkflowStage.COMPLETED
        }
        self.artifacts: Dict[str, Any] = {}
        self.error: Optional[str] = None
        self.started_at: Optional[datetime] = None
        self.updated_at: datetime = datetime.now()
        self.meta: Dict[str, Any] = {}
        self.stage_progress: Dict[str, Dict[str, Any]] = {}
        # 执行归属凭据：引擎将本会话某阶段置 running 的进程启动标识（跨进程必陈旧）。
        # 仅用于恢复判定，不进 /status 快照（to_dict 不含此字段）。
        self.boot_id: Optional[str] = None
        # DB 已提交视图的元信息：内容 revision、预览层、执行层、存在工作稿的阶段
        self.revision: int = 0
        self.previews: Dict[str, Any] = {}
        self.executions: Dict[str, Any] = {}
        self.working_stages: Set[str] = set()

    def to_dict(self) -> Dict:
        execution_preview = {}
        for stage, execution in self.executions.items():
            preview = workflow_store.execution_preview(stage, execution)
            if preview:
                execution_preview[stage] = preview
        return {
            "session_id": self.session_id,
            "owner": self.owner,
            "current_stage": self.current_stage.value,
            "status": copy.deepcopy(self.status),
            "error": self.error,
            "artifacts": copy.deepcopy(self.artifacts),
            "meta": copy.deepcopy(self.meta),
            "stage_progress": copy.deepcopy(self.stage_progress),
            "updated_at": self.updated_at,
            # 上次未完成执行的条目摘要：只读，不是成功快照，也不推进停点
            "execution_preview": execution_preview,
        }


# _mutate 的变更函数返回此哨兵表示“无需提交”
_NO_COMMIT = object()

# 阶段状态“待重新确认”：产物已完成但输入已变（openspec: script-manual-edit-resync）。
# 不复用 waiting 是因为 `_recalculate_all_statuses` 不跳过 waiting：下一次上传/保存选择
# 就会把标记冲回 completed；也不新增布尔字段，避开快照读写与各消费方加分支。
STATUS_RECONFIRM = "reconfirm"

# 标记时写入 stage_progress 的过期原因（UI 元数据，阶段执行时被整体覆盖清除）
STALE_REASON_SCRIPT = "script_structure_changed"

# 执行期间用户可编辑、结束合并与运行中展示层均优先保留的条目字段。
# `status` 同样在内：执行期间上传会把条目改判 done，不收尾保护则会被本轮 payload 标回 failed
# （有图却显示失败+重试，诱导不必要的模型调用；openspec: running-stage-write-visibility D9）。
_USER_EDIT_FIELDS = ("selected", "name", "description", "species", "duration", "rewritten_prompt", "manual_prompt",
                     "status")

# 后台单条重生成时按阶段保护的用户手改字段（A1b/B2）。
# 只纳入“存在用户编辑入口且保留用户值不使下游语义失真”的字段：
# - description（角色/场景）与 rewritten_prompt（H3）都有真实编辑入口；
# - name 是下游匹配键（consumption/segment.characters/参考图引用），不保护；
# - species 在脚本阶段编辑、属跨阶段传播，不保护。
_BACKGROUND_PROTECTED_FIELDS: Dict[str, tuple] = {
    "character_design": ("description",),
    "prompt_rewrite": ("rewritten_prompt",),
}

# 区分“无基线信息”与“基线里字段值本就为空”：前者无法判定执行期间是否手改
_NO_BASELINE = object()


class _StageRun:
    """一次阶段执行的可信上下文（仅内存）：execution_id、发布器、条目事件序号与受管暂存。"""

    def __init__(self, *, session_id: str, stage: "WorkflowStage", execution_id: str,
                 background: bool, intervention: Optional[Dict]) -> None:
        self.session_id = session_id
        self.stage = stage
        self.execution_id = execution_id
        self.background = background
        self.intervention = intervention
        self.owner = ""
        self.start_artifacts: Dict[str, Any] = {}
        # 本次执行的字段基线（懒计算一次；所有事件与收尾看到同一份）
        self.baseline_fields: Optional[Dict[str, Any]] = None
        self.publisher: Optional[workflow_store.ExecutionPublisher] = None
        self.active_registered = False
        self.finished = False
        self.staged: List[Any] = []
        self.lock = threading.RLock()
        self._seq: Dict[str, int] = {}

    def next_seq(self, key: str) -> int:
        with self.lock:
            self._seq[key] = self._seq.get(key, 0) + 1
            return self._seq[key]


class WorkflowEngine:
    """工作流引擎 - 管理六阶段状态机"""

    def __init__(self):
        self.agent_factories = {
            WorkflowStage.SCRIPT_GENERATION: ScriptWriterAgent,
            WorkflowStage.CHARACTER_DESIGN: CharacterDesignerAgent,
            WorkflowStage.STORYBOARD: StoryboardAgent,
            WorkflowStage.REFERENCE_GENERATION: ReferenceGeneratorAgent,
            WorkflowStage.PROMPT_REWRITE: PromptRewriteAgent,
            WorkflowStage.VIDEO_GENERATION: VideoDirectorAgent,
            WorkflowStage.POST_PRODUCTION: VideoEditorAgent,
        }
        self.sessions: Dict[str, WorkflowState] = {}
        self._stop_events: Dict[str, threading.Event] = {}
        self._active_sessions: Set[str] = set()
        self._background_tasks: Set[Any] = set()
        self._state_lock = threading.RLock()
        # 旧 JSON 会话目录：仅显式 legacy 导入使用，引擎不再读写（DB 是唯一权威）。
        self._session_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..', 'code', 'data', 'sessions'
        )
        # 进程启动标识：一次性生成，作为跨进程陈旧 running 的归属判据（D1）。
        self._boot_id = uuid.uuid4().hex
        # 提交/缓存服务与每会话提交互斥（短事务；不同会话并行）
        self._persist = PersistenceService(loader=load_committed)
        self._session_locks: Dict[str, threading.RLock] = {}
        # 本进程运行中的覆盖层（进度/条目预览）：只用于展示，不落库、不作成功快照
        self._overlays: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._errors: Dict[str, Optional[str]] = {}
        # 不在 import/构造期读 DB：lifespan 完成 init_db 后再调用 load_sessions()。

    # ──────────── DB 读模型与提交 ────────────

    def _session_lock(self, session_id: str) -> threading.RLock:
        with self._state_lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._session_locks[session_id] = lock
            return lock

    def _state_from_snapshot(self, snapshot: Dict[str, Any]) -> WorkflowState:
        state = WorkflowState(session_id=str(snapshot["session_id"]))
        state.owner = str(snapshot.get("owner") or getattr(settings, "USERS_LEGACY_OWNER", "local"))
        try:
            state.current_stage = WorkflowStage(snapshot.get("current_stage") or "init")
        except ValueError:
            state.current_stage = WorkflowStage.INIT
        for key, value in (snapshot.get("status_json") or {}).items():
            # 旧 sweep 写入的 interrupted 与前端状态口径对齐为 stopped
            state.status[key] = "stopped" if value == "interrupted" else value
        state.stage_progress = copy.deepcopy(snapshot.get("stage_progress_json") or {})
        state.meta = workflow_store.session_meta(snapshot.get("meta_json"))
        state.boot_id = snapshot.get("boot_id")
        updated = snapshot.get("updated_at")
        state.updated_at = datetime.fromtimestamp(int(updated) / 1000) if updated else datetime.now()
        state.artifacts, state.previews, state.executions = workflow_store.view_layers(snapshot)
        state.working_stages = {
            stage for stage, layer in (snapshot.get("working") or {}).items()
            if isinstance(layer, dict) and layer.get("working") is not None
        }
        state.revision = int(snapshot.get("revision") or 0)
        current = state.executions.get(state.current_stage.value)
        if state.status.get(state.current_stage.value) == "error" and isinstance(current, dict):
            state.error = current.get("error")
        return state

    def _install(self, snapshot: Dict[str, Any]) -> WorkflowState:
        state = self._state_from_snapshot(snapshot)
        with self._state_lock:
            current = self.sessions.get(state.session_id)
            if current is None or current.revision <= state.revision:
                self.sessions[state.session_id] = state
            else:
                state = current
        return state

    def _evict(self, session_id: str) -> None:
        with self._state_lock:
            self.sessions.pop(session_id, None)
        self._persist.cache.evict(session_id)

    def _load(self, session_id: str, *, recover: bool = True) -> Optional[WorkflowState]:
        """已提交视图：缓存命中直接返回，否则从 DB 装载；DB 无行即不存在（不读同名 JSON）。"""
        with self._state_lock:
            cached = self.sessions.get(session_id)
        if cached is not None:
            return cached
        try:
            snapshot = load_committed(session_id)
        except CommitParentMissing:
            return None
        state = self._install(snapshot)
        if recover and self._needs_recovery(state):
            state = self._recover(session_id) or state
        return state

    def _require(self, session_id: str) -> WorkflowState:
        state = self._load(session_id)
        if state is None:
            raise KeyError(f"Session not found: {session_id}")
        return state

    def _needs_recovery(self, state: WorkflowState) -> bool:
        if any(value == "running" for value in state.status.values()) and state.boot_id != self._boot_id:
            return True
        return any(
            isinstance(execution, dict)
            and execution.get("status") == "running"
            and execution.get("boot_id") != self._boot_id
            for execution in state.executions.values()
        )

    def _recover(self, session_id: str) -> Optional[WorkflowState]:
        """上一进程遗留的 running：状态归一为 stopped，执行层以 interrupted 结束（active→last）。"""
        reason = "服务重启导致执行中断（非用户手动停止）"

        def execution_states(after: WorkflowState) -> Dict[str, Dict[str, Any]]:
            return {
                stage: {"action": "finish", "execution_id": execution.get("execution_id"),
                        "status": "interrupted", "error": reason}
                for stage, execution in after.executions.items()
                if isinstance(execution, dict) and execution.get("status") == "running"
                and execution.get("boot_id") != self._boot_id and execution.get("execution_id")
            }

        try:
            state, _ = self._mutate(
                session_id, self._recover_stale_running, execution_states=execution_states,
            )
            return state
        except Exception:
            logger.exception("stale-running-recovery failed: session=%s", session_id)
            return None

    def _mutate(
        self,
        session_id: str,
        fn: Callable[[WorkflowState], Any],
        *,
        published: Optional[Dict[str, Any]] = None,
        execution_states: Any = None,
        execution_deltas: Optional[Dict[str, Dict[str, Any]]] = None,
        publisher: Optional["workflow_store.ExecutionPublisher"] = None,
        publish_stage: str = "",
    ) -> tuple:
        """读最新已提交视图 → 在副本上变更 → 发布新媒体 → CAS 提交 → 安装缓存。

        fn 返回 _NO_COMMIT 表示无需提交；其余返回值原样带回。共享 state 不被就地修改。
        """
        with self._session_lock(session_id):
            before = self._load(session_id, recover=False)
            if before is None:
                raise KeyError(f"Session not found: {session_id}")
            after = copy.deepcopy(before)
            outcome = fn(after)
            if outcome is _NO_COMMIT and not execution_deltas and not published:
                return before, outcome
            states = execution_states(after) if callable(execution_states) else (execution_states or {})
            state = self._commit(
                before, after,
                published=published,
                execution_states=states,
                execution_deltas=execution_deltas or {},
                publisher=publisher,
                publish_stage=publish_stage,
            )
            return state, outcome

    def _commit(
        self,
        before: WorkflowState,
        after: WorkflowState,
        *,
        published: Optional[Dict[str, Any]],
        execution_states: Dict[str, Dict[str, Any]],
        execution_deltas: Dict[str, Dict[str, Any]],
        publisher: Optional["workflow_store.ExecutionPublisher"],
        publish_stage: str,
    ) -> WorkflowState:
        sid = before.session_id
        for stage, artifact in list(after.artifacts.items()):
            after.artifacts[stage] = workflow_store.strip_internal(artifact)
        changed = [
            stage for stage, artifact in after.artifacts.items()
            if artifact != before.artifacts.get(stage)
        ]
        carried = workflow_store.carried_media(before.artifacts, before.executions)
        carried |= workflow_store.published_paths(published)
        extra = None
        if changed:
            pub = publisher or workflow_store.ExecutionPublisher(
                owner=before.owner,
                session_id=sid,
                stage=publish_stage or changed[0],
                execution_id=new_execution_id(),
                boot_id=self._boot_id,
            )
            mapped, extra = pub.publish({stage: after.artifacts[stage] for stage in changed}, carried)
            after.artifacts.update(mapped)
        combined = workflow_store.merge_published(published, extra)

        changes = workflow_store.build_stage_changes(
            before.artifacts, after.artifacts, has_working=before.working_stages,
        )
        by_stage = {change["stage"]: change for change in changes}
        for stage, delta in execution_deltas.items():
            by_stage.setdefault(stage, {"stage": stage})["execution"] = delta
        for stage, state_change in execution_states.items():
            by_stage.setdefault(stage, {"stage": stage})["execution_state"] = state_change
        stage_changes = list(by_stage.values())

        files: List[Dict[str, Any]] = []
        for result in (published, extra):
            if result:
                files.extend(build_file_rows(
                    result, owner=before.owner, resource_kind="sessions", resource_id=sid,
                    stage=str(result.get("stage") or publish_stage or ""),
                ))
        patch = self._session_patch(before, after)
        if not stage_changes and not patch and not files:
            return before
        try:
            snapshot = self._persist.commit(
                published=combined,
                session_id=sid,
                owner=before.owner,
                expected_revision=before.revision,
                stage_changes=stage_changes,
                files=files,
                session_patch=patch,
                retained_operation_ids=(combined or {}).get("operation_ids") or [],
            )
        except Exception:
            self._evict(sid)
            raise
        try:
            return self._install(snapshot)
        except Exception:
            # DB 已提交：缓存安装失败只丢缓存，按 DB 重新装载（不当作提交失败）
            logger.exception("cache install failed after commit: %s", sid)
            self._evict(sid)
            fresh = self._load(sid, recover=False)
            if fresh is None:
                raise
            return fresh

    @staticmethod
    def _status_column(state: WorkflowState) -> str:
        return str(state.status.get(state.current_stage.value) or "idle")

    def _session_patch(self, before: WorkflowState, after: WorkflowState) -> Dict[str, Any]:
        patch: Dict[str, Any] = {}
        if after.status != before.status:
            patch["status_json"] = copy.deepcopy(after.status)
        if after.current_stage != before.current_stage:
            patch["current_stage"] = after.current_stage.value
        if after.stage_progress != before.stage_progress:
            patch["stage_progress_json"] = copy.deepcopy(after.stage_progress)
        if after.meta != before.meta:
            patch["meta_json"] = copy.deepcopy(after.meta)
        if after.boot_id != before.boot_id:
            patch["boot_id"] = after.boot_id
        if self._status_column(after) != self._status_column(before):
            patch["status"] = self._status_column(after)
        return patch

    # ──────────── 运行中覆盖层（仅展示） ────────────

    def _overlay(self, session_id: str, stage: str) -> Dict[str, Any]:
        with self._state_lock:
            return self._overlays.setdefault(session_id, {}).setdefault(
                stage, {"progress": None, "preview": None, "items": {},
                        "base": None, "baseline_fields": None}
            )

    def _clear_overlay(self, session_id: str, stage: str) -> None:
        with self._state_lock:
            stages = self._overlays.get(session_id)
            if stages is not None:
                stages.pop(stage, None)
                if not stages:
                    self._overlays.pop(session_id, None)

    @classmethod
    def _merge_asset_update(cls, artifact: Dict[str, Any], asset_update: Dict[str, Any],
                            baseline_fields: Optional[Dict[str, Any]] = None) -> None:
        """单条目事件合入产物副本（运行覆盖层与后台重生成提交共用同一口径）。

        `baseline_fields` 为执行开始时的字段快照（`{container: {id: {field: value}}}`）：
        提供时，执行期间被改过的 `selected` 保留当前值；不提供（旧数据/降级路径）
        则以本次结果为准。运行中覆盖层与后台重生成均传同一份基线（design D5）。
        """
        item_type = asset_update.get("type")
        item_id = asset_update.get("id")
        if not item_type or not item_id:
            return
        baseline_map = (baseline_fields or {}).get(item_type) or {}
        items = artifact.setdefault(item_type, [])
        if not isinstance(items, list):
            items = []
            artifact[item_type] = items
        for item in items:
            if isinstance(item, dict) and item.get("id") == item_id:
                next_status = asset_update.get("status", item.get("status"))
                if item.get("selected") and next_status in {"done", "failed"}:
                    next_status = "done"
                item["status"] = next_status
                if "selected" in asset_update:
                    item["selected"] = cls._selected_after_asset_update(
                        item.get("selected"), asset_update.get("selected"),
                        baseline_map.get(item_id, {}).get("selected", _NO_BASELINE),
                    )
                if "versions" in asset_update:
                    item["versions"] = cls._merge_asset_versions(item.get("versions"), asset_update.get("versions"))
                for key in ("rewrite_result", "evaluation", "error_type", "error"):
                    if key in asset_update:
                        item[key] = copy.deepcopy(asset_update[key])
                return
        new_item = {
            "id": item_id,
            "status": asset_update.get("status", "done"),
            "selected": asset_update.get("selected", ""),
            "versions": asset_update.get("versions", []),
        }
        for key in ("rewrite_result", "evaluation", "error_type", "error"):
            if key in asset_update:
                new_item[key] = copy.deepcopy(asset_update[key])
        items.append(new_item)

    @staticmethod
    def _merge_overlay_preview(stage: str, committed: Dict[str, Any], preview: Any,
                               base: Optional[Dict[str, Any]]) -> Any:
        """运行中预览合入已提交视图：执行期间的用户写入不得被预览旧值遮蔽（design D3）。

        字段与条目集合的判定口径与收尾层 `_prefer_user_edits` 保持一致：
        - 已提交值偏离基线 ⇒ 执行期间用户写入 ⇒ 取已提交值；
        - 基线缺该字段 ⇒ 仅当已提交值非 None 才算用户新增（同收尾层，避免把
          `species: null` 这类键带进对外产物）；`_field_edited_by_user` 在基线缺字段时
          返回 False，因此不能只调它；
        - 与基线一致 ⇒ 保留预览值，运行中的 pending/running 实时进度不回退。
        只处理 `ITEM_CONTAINERS` 列出的条目容器；`storyboard` 的剧集/片段不属条目容器
        （预览带 persist 与已提交同源，字段比较分不清谁新，design D4）。
        预览已经 `map_media` + `strip_internal`，与已提交视图同为 public_path 形态，可直接比较；
        派生字段（`needs_regeneration`/`prompt_source`）由 `_view_dict` 末尾 annotate 重算，不搬运。
        """
        if not isinstance(preview, dict):
            return copy.deepcopy(preview)
        merged = copy.deepcopy(preview)
        if not isinstance(base, dict):
            # 无基线（旧数据/降级路径）：无法判定手改，维持改动前的"预览整体呈现"
            return merged
        committed = committed if isinstance(committed, dict) else {}
        for container in workflow_store.ITEM_CONTAINERS.get(stage, ()):
            preview_items = merged.get(container)
            committed_items = committed.get(container)
            if not isinstance(preview_items, list) or not isinstance(committed_items, list):
                continue
            base_by_id = {
                item.get("id"): item for item in base.get(container) or []
                if isinstance(item, dict) and item.get("id")
            }
            committed_by_id = {
                item.get("id"): item for item in committed_items
                if isinstance(item, dict) and item.get("id")
            }
            for item in preview_items:
                if not isinstance(item, dict):
                    continue
                current = committed_by_id.get(item.get("id"))
                if not isinstance(current, dict):
                    continue
                original = base_by_id.get(item.get("id")) or {}
                for field in _USER_EDIT_FIELDS:
                    if field not in current:
                        continue
                    if field not in original:
                        if current[field] is not None:
                            item[field] = copy.deepcopy(current[field])
                    elif WorkflowEngine._field_edited_by_user(current, original, field):
                        item[field] = copy.deepcopy(current[field])
                merged_versions = WorkflowEngine._merge_asset_versions(
                    current.get("versions"), item.get("versions"),
                )
                if merged_versions or "versions" in item:
                    item["versions"] = merged_versions
            # 执行期间新写入的条目（committed 有、预览无、基线也无）MUST 可见；
            # 基线有而预览无 = 本轮主动排除，MUST NOT 回填（同 _prefer_user_edits 判据）
            present = {item.get("id") for item in preview_items if isinstance(item, dict)}
            for item_id, current in committed_by_id.items():
                if item_id not in present and item_id not in base_by_id:
                    preview_items.append(copy.deepcopy(current))
        return merged

    def _view_dict(self, state: WorkflowState, annotate: bool = True) -> Dict[str, Any]:
        data = state.to_dict()
        with self._state_lock:
            overlays = copy.deepcopy(self._overlays.get(state.session_id) or {})
            error = self._errors.get(state.session_id)
        for stage, overlay in overlays.items():
            if overlay.get("progress"):
                data["stage_progress"][stage] = overlay["progress"]
            committed = copy.deepcopy(data["artifacts"].get(stage) or {})
            preview = overlay.get("preview")
            if preview is not None:
                artifact = self._merge_overlay_preview(stage, committed, preview, overlay.get("base"))
            elif overlay.get("items"):
                artifact = committed
            else:
                artifact = None
            if artifact is not None:
                # 预览与条目事件都按执行开始基线合入：执行期间的用户写入即时可见，
                # 且不会被晚到的本轮结果顶掉（design D3/D5；落库口径仍由收尾层保证）。
                if isinstance(artifact, dict):
                    for update in overlay.get("items", {}).values():
                        self._merge_asset_update(artifact, update, overlay.get("baseline_fields"))
                data["artifacts"][stage] = artifact
        if error and not data.get("error"):
            data["error"] = error
        # WF-04：读时派生“需重生成”（不落库；配置/素材变化自动覆盖）
        if annotate:
            try:
                from core.prompt_freshness import annotate_regeneration

                annotate_regeneration(data.get("artifacts") or {}, getattr(state, "meta", None))
            except Exception as exc:  # noqa: BLE001 - 派生失败不影响快照
                logger.warning("needs_regeneration 派生失败：%s", exc)
        return data

    # ──────────── 会话读取 ────────────

    def get_or_create_state(self, session_id: str) -> WorkflowState:
        """兼容旧入口：会话必须已在 DB 中创建，不再隐式建空会话。"""
        validate_upload_identifier(str(session_id), "session_id")
        state = self._require(session_id)
        self.get_stop_event(session_id)
        return state

    def get_state(self, session_id: str) -> Optional[WorkflowState]:
        if not _is_valid_upload_identifier(session_id):
            logger.warning("Invalid session identifier rejected before DB read: %r", session_id)
            return None
        return self._load(session_id)

    def _recover_stale_running(self, state: WorkflowState) -> bool:
        """将无主的陈旧 running 阶段归一为 stopped（可重做的终态），产物/versions/selected 不动。

        判据（D1）：阶段为 running 且其归属凭据与当前进程不一致（含旧会话缺 boot_id）。
        有主（boot_id == 当前进程，含本进程内正在跑）不改。同进程内“boot_id 匹配但执行悬空”
        属 workflow-poll-timeout-visibility 职责，本函数不处理（D6）。MUST 在 _state_lock 内调用。
        返回是否发生了改动（幂等：归一后不再是 running，重复调用自然 no-op）。
        """
        changed = False
        for stage_value, st in list(state.status.items()):
            if st == "running" and state.boot_id != self._boot_id:
                state.status[stage_value] = "stopped"
                reason = "服务重启导致执行中断（非用户手动停止）"
                prog = state.stage_progress.setdefault(stage_value, {})
                prog["step"] = reason
                prog["message"] = reason
                prog["interrupted_by_restart"] = True
                changed = True
                logger.warning(
                    "stale-running-recovery: session=%s stage=%s running→stopped "
                    "(owner boot_id=%s, current=%s)",
                    state.session_id, stage_value, state.boot_id, self._boot_id,
                )
        if changed:
            state.boot_id = None
            state.error = None
            state.updated_at = datetime.now()
        return changed

    def get_stop_event(self, session_id: str) -> threading.Event:
        with self._state_lock:
            if session_id not in self._stop_events:
                self._stop_events[session_id] = threading.Event()
            return self._stop_events[session_id]

    def create_session(self, session_id: str, meta: Dict[str, Any], owner: str = "") -> Dict[str, Any]:
        """Create the session row in the DB (insert-only); an existing session only gets new meta."""
        validate_upload_identifier(str(session_id), "session_id")
        owner = str(owner or getattr(settings, "USERS_LEGACY_OWNER", "local"))
        normalized = {k: _normalize_meta_value(v) for k, v in copy.deepcopy(meta or {}).items() if v is not None}
        status = WorkflowState(session_id).status
        status[WorkflowStage.INIT.value] = "completed"
        created = create_session_row(
            session_id=session_id,
            owner=owner,
            current_stage=WorkflowStage.INIT.value,
            status_json=status,
            meta_json=normalized,
            status="completed",
        )
        if created:
            self._evict(session_id)
            state = self._require(session_id)
        else:
            def apply(after: WorkflowState) -> None:
                # 同毫秒会话号撞到他人会话：拒绝，不得改写别人的 meta
                if after.owner != owner:
                    raise CommitConflict(f"session id already taken: {session_id}")
                after.status[after.current_stage.value] = "completed"
                after.meta.update(normalized)

            state, _ = self._mutate(session_id, apply)
        self.get_stop_event(session_id)
        return {
            "session_id": session_id,
            "status": copy.deepcopy(state.status),
            "meta": copy.deepcopy(state.meta),
        }

    def get_status_snapshot(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return a deep-copied session snapshot: committed DB view plus this process' running overlay."""
        state = self.get_state(session_id)
        return self._view_dict(state) if state else None

    def get_artifact_snapshot(self, session_id: str, stage: str) -> Optional[Any]:
        """Return a deep-copied artifact snapshot from the unified read model."""
        state = self.get_state(session_id)
        if not state:
            raise KeyError(f"Session not found: {session_id}")
        artifact = self._view_dict(state)["artifacts"].get(stage)
        return copy.deepcopy(artifact) if artifact is not None else None

    def update_session_meta(self, session_id: str, updates: Dict[str, Any], allowed_keys: tuple[str, ...]) -> Dict[str, Any]:
        """Update session-level generation settings through one committed change."""
        def apply(after: WorkflowState) -> None:
            if not after.meta:
                after.meta = {}
            previous_mode = after.meta.get("video_generation_mode")
            for key in allowed_keys:
                if key in updates:
                    after.meta[key] = updates[key]
            if "video_generation_mode" in updates:
                mode = str(after.meta.get("video_generation_mode") or "first_frame")
                if mode != previous_mode:
                    # 先对齐标记、后重算：保证快照晋升与状态判定看到同一份产物（design D3/D7）
                    self._align_reference_skip(after, mode)
                    self._recalculate_all_statuses(after)

        state, _ = self._mutate(session_id, apply)
        return {"status": "ok", "meta": copy.deepcopy(state.meta)}

    def _align_reference_skip(self, state: WorkflowState, mode: str) -> None:
        """按当前模式双向对齐参考图阶段的 `skip` 标记（reference-mode-skip-scene-images D3/D7）。

        - `reference`：产物存在即确保标记存在（不检查是否有可用选中图）；产物不存在时不创建；
        - 其他模式：一律清除标记（该标记只对 reference 模式有效）。
        """
        artifact = state.artifacts.get(WorkflowStage.REFERENCE_GENERATION.value)
        if not isinstance(artifact, dict) or not artifact:
            return
        if mode == "reference":
            artifact["skip"] = build_reference_skip(mode)
        else:
            artifact.pop("skip", None)

    @staticmethod
    def _reference_has_selection(state: WorkflowState) -> bool:
        """参考图产物中是否存在可用的选中图（无产物/全空/全占位 → False）。"""
        artifact = state.artifacts.get(WorkflowStage.REFERENCE_GENERATION.value)
        scenes = artifact.get("scenes") if isinstance(artifact, dict) else []
        return any(
            isinstance(item, dict) and str(item.get("selected") or "").strip()
            for item in scenes or []
        )

    def _ensure_stage_seeded(self, session_id: str, stage: str) -> None:
        if stage != WorkflowStage.CHARACTER_DESIGN.value:
            return

        def seed(after: WorkflowState) -> Any:
            # 种入条目立即提交：预检失败触发上传兜底时条目必须已可见
            return None if self._seed_stage_items_from_script(after) else _NO_COMMIT

        self._mutate(session_id, seed)

    def _ensure_session_language(self, session_id: str) -> None:
        """首次阶段执行前回填会话语言（写路径，不在 GET/status 推断）。"""
        state = self.get_state(session_id)
        if not state:
            return
        meta = state.meta or {}
        if language_from_meta(meta):
            return
        script = state.artifacts.get(WorkflowStage.SCRIPT_GENERATION.value) or {}
        script_meta = script.get("meta") if isinstance(script.get("meta"), dict) else {}
        body = (script_meta.get("original_text") or script.get("original_text")
                or meta.get("idea") or "")
        title = script.get("title") or ""
        instruction = meta.get("user_textbox_input") or ""
        resolved = resolve_language(
            instruction=instruction, body=body, title=title, current=meta.get("language"),
        )
        self.update_session_meta(
            session_id,
            {"language": resolved["language"], "language_source": resolved["source"]},
            ("language", "language_source"),
        )

    def prepare_stage_execution(self, session_id: str, stage: str, body: Dict[str, Any]) -> tuple[WorkflowState, Dict[str, Any]]:
        """Build stage input from the committed meta without exposing mutable state to routers.

        媒体物化不在这里做：execute_stage 在完整快照注入后按 D14 消费计划只物化实际读取的字段。
        """
        validate_upload_identifier(str(session_id), "session_id")
        self._ensure_stage_seeded(session_id, stage)
        self._ensure_session_language(session_id)
        state = self.get_or_create_state(session_id)
        input_data = copy.deepcopy(body) if isinstance(body, dict) else {}
        input_data["session_id"] = session_id
        for key, value in copy.deepcopy(state.meta).items():
            if value is not None and (key not in input_data or not input_data[key]):
                input_data[key] = value
        return state, input_data

    def prepare_intervention_execution(
        self,
        session_id: str,
        stage: str,
        modifications: Dict[str, Any],
    ) -> tuple[WorkflowState, Dict[str, Any]]:
        """Build intervention input from the latest committed artifact/meta snapshot."""
        state = self.get_state(session_id)
        if not state:
            raise KeyError(f"Session not found: {session_id}")
        self._ensure_stage_seeded(session_id, stage)
        self._ensure_session_language(session_id)
        state = self._require(session_id)
        current_artifact = copy.deepcopy(state.artifacts.get(stage, {}))
        input_data = current_artifact if isinstance(current_artifact, dict) else {}
        input_data["session_id"] = session_id
        for key, value in copy.deepcopy(state.meta).items():
            if value is not None and key not in input_data:
                input_data[key] = value
        input_data.update(modifications or {})
        return state, input_data

    @staticmethod
    def _inject_user_selections(artifacts: Dict[str, Any], stage: str, data: Dict[str, Any]):
        """Inject persisted user choices into downstream stage input."""
        if stage == 'video_generation' and 'selected_images' not in data:
            ref_art = artifacts.get('reference_generation', {})
            if isinstance(ref_art, dict):
                scenes = ref_art.get('scenes', [])
                selected_images = {
                    s['id']: s['selected']
                    for s in scenes
                    if isinstance(s, dict) and s.get('id') and s.get('selected')
                }
                if selected_images:
                    data['selected_images'] = selected_images

        if stage == 'video_generation' and 'clips' not in data:
            vid_art = artifacts.get('video_generation', {})
            if isinstance(vid_art, dict):
                clips = vid_art.get('clips', [])
                if clips:
                    data['clips'] = clips

        if stage == 'post_production' and 'selected_clips' not in data:
            vid_art = artifacts.get('video_generation', {})
            if isinstance(vid_art, dict):
                clips = vid_art.get('clips', [])
                selected_clips = {
                    c['id']: c['selected']
                    for c in clips
                    if isinstance(c, dict) and c.get('id') and c.get('selected')
                }
                if selected_clips:
                    data['selected_clips'] = selected_clips

    def _seed_stage_items_from_script(self, state: WorkflowState) -> bool:
        """第 2 阶段无产物条目时，从剧本产物种子化角色/场景条目（pending、空版本）。

        刻意不调用 _recalculate_all_statuses：避免阶段在执行前就从 pending 变 waiting；
        返回是否有变更（需要落盘）。
        """
        stage_key = WorkflowStage.CHARACTER_DESIGN.value
        artifact = state.artifacts.get(stage_key)
        if isinstance(artifact, dict) and (artifact.get("characters") or artifact.get("settings")):
            return False

        script_art = state.artifacts.get(WorkflowStage.SCRIPT_GENERATION.value)
        if not isinstance(script_art, dict):
            return False
        characters = script_art.get("characters") or []
        settings = script_art.get("settings") or []
        if not characters and not settings:
            return False

        seeded = artifact if isinstance(artifact, dict) else {"characters": [], "settings": [], "version": 1}
        seeded.setdefault("characters", [])
        seeded.setdefault("settings", [])
        changed = False

        existing_char_ids = {
            item.get("id") for item in seeded["characters"] if isinstance(item, dict)
        }
        for char in characters:
            if not isinstance(char, dict):
                continue
            # 缺 character_id 时按名称派生主键，不能直接丢条目：否则阶段 2 既无条目也取不到描述
            char_id = script_revision.item_id(char, "character_id", "char")
            if not char_id or char_id in existing_char_ids:
                continue
            seeded["characters"].append({
                "id": char_id,
                "name": char.get("name", ""),
                "description": char.get("description", ""),
                "species": char.get("species", ""),
                "asset_kind": asset_kind_for(char.get("asset_kind") or char.get("species"), container="characters"),
                "selected": "",
                "versions": [],
                "status": "pending",
            })
            existing_char_ids.add(char_id)
            changed = True

        existing_set_ids = {
            item.get("id") for item in seeded["settings"] if isinstance(item, dict)
        }
        for setting in settings:
            if not isinstance(setting, dict):
                continue
            setting_id = script_revision.item_id(setting, "setting_id", "set")
            if not setting_id or setting_id in existing_set_ids:
                continue
            seeded["settings"].append({
                "id": setting_id,
                "name": setting.get("name", ""),
                "description": setting.get("description", ""),
                "asset_kind": asset_kind_for(setting.get("asset_kind") or setting.get("species"), container="settings"),
                "selected": "",
                "versions": [],
                "status": "pending",
            })
            existing_set_ids.add(setting_id)
            changed = True

        if changed:
            state.artifacts[stage_key] = seeded
            logger.info(
                "[seed] character_design seeded from script: chars=%d settings=%d",
                len(seeded["characters"]),
                len(seeded["settings"]),
            )
        return changed

    def _rebuild_stage_items_from_script(self, state: WorkflowState) -> bool:
        """剧本变更后（应用修订或手动编辑），按新角色/场景集合重建阶段 2 条目。

        只在阶段 2 已存在产物时执行；条目匹配先按主键、主键未命中再按归一化名称，
        因此剧本页改名不会退化成“删旧+加新”而丢掉已生成资产；新条目沿用剧本侧
        character_id/setting_id（缺失时按与种子化同源的名称派生并回写剧本产物），
        消失的条目移出产物（磁盘文件不动）。
        """
        stage_key = WorkflowStage.CHARACTER_DESIGN.value
        artifact = state.artifacts.get(stage_key)
        if not isinstance(artifact, dict):
            return False
        script_art = state.artifacts.get(WorkflowStage.SCRIPT_GENERATION.value)
        if not isinstance(script_art, dict):
            return False

        changed = False
        for list_key, source_id_key in (
            ("characters", "character_id"),
            ("settings", "setting_id"),
        ):
            source_items = script_art.get(list_key)
            if list_key not in script_art:
                # 剧本产物本身不带这一维度（如旧会话只有 characters）：不拿空集合去清对应条目
                continue
            if source_items is None:
                # 显式 null 等价“清空该维度”：与结构指纹对 None 按 [] 计的口径保持一致，
                # 否则指纹判为变更而重建跳过，阶段 2 会留下剧本已删的条目
                source_items = []
            if not isinstance(source_items, list):
                continue
            existing_items = artifact.get(list_key)
            if not isinstance(existing_items, list):
                existing_items = []
            existing_by_id = {
                str(item.get("id")): item
                for item in existing_items
                if isinstance(item, dict) and item.get("id")
            }
            existing_by_name = {
                script_revision.normalize_name(item.get("name")): item
                for item in existing_items
                if isinstance(item, dict) and item.get("name")
            }
            prefix = "char" if list_key == "characters" else "set"
            consumed: set = set()
            rebuilt = []
            for item in source_items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                # 主键优先：剧本条目已带主键且与阶段 2 同 id 时直接命中（改名保资产）；
                # 未命中再按名称兜底（旧会话条目可能完全没有主键）。
                own_id = str(item.get(source_id_key) or "").strip()
                previous = existing_by_id.get(own_id) if own_id else None
                if previous is None:
                    previous = existing_by_name.get(script_revision.normalize_name(name)) or {}
                item_id = str(previous.get("id") or own_id or "")
                if not item_id:
                    item_id = script_revision.item_id(item, source_id_key, prefix)
                if not item_id or item_id in consumed:
                    # 派生碰撞或同名多条目：退回随机主键，保证一条目一主键
                    item_id = f"{prefix}_{uuid.uuid4().hex[:6]}"
                consumed.add(item_id)
                if item.get(source_id_key) != item_id:
                    item[source_id_key] = item_id
                    changed = True
                rebuilt_item = {
                    **copy.deepcopy(previous),
                    "id": item_id,
                    "name": name,
                    "description": item.get("description", ""),
                    "selected": previous.get("selected", ""),
                    "versions": copy.deepcopy(previous.get("versions") or []),
                    "status": previous.get("status", "pending"),
                }
                if list_key == "characters":
                    rebuilt_item["species"] = item.get(
                        "species", previous.get("species", "")
                    )
                # 资产路由字段跟随剧本条目重派生（species 优先）：否则用户改物种后
                # 阶段 2 仍按旧主体类型出图
                rebuilt_item["asset_kind"] = asset_kind_for(
                    item.get("species") or item.get("asset_kind") or rebuilt_item.get("species"),
                    container=list_key,
                )
                rebuilt.append(rebuilt_item)

            if rebuilt != existing_items:
                artifact[list_key] = rebuilt
                changed = True

        if changed:
            state.artifacts[stage_key] = artifact
            logger.info(
                "[revision] character_design rebuilt from script: chars=%d settings=%d",
                len(artifact.get("characters") or []),
                len(artifact.get("settings") or []),
            )
        return changed

    @staticmethod
    def _reconfirm_stage_keys(*, from_stage: WorkflowStage) -> List[str]:
        """参与待重新确认标记的阶段区间：启用序列中 `from_stage` 起、到后期制作之前。

        按 `get_stage_order()` 取，因此可选注入阶段（prompt_rewrite）启用时自然参与标记，
        禁用时不产生任何键；MUST NOT 用固定“阶段 3/4/5”编号硬编码。
        """
        order = get_stage_order()
        try:
            start = order.index(from_stage)
        except ValueError:
            return []
        keys: List[str] = []
        for stage in order[start:]:
            if stage in (WorkflowStage.POST_PRODUCTION, WorkflowStage.INIT, WorkflowStage.COMPLETED):
                continue
            keys.append(stage.value)
        return keys

    def _mark_downstream_reconfirm(
        self,
        state: WorkflowState,
        stage_keys: List[str],
        stale_episodes: Optional[List[int]] = None,
        *,
        baseline: Optional[Dict[str, str]] = None,
    ) -> bool:
        """把已执行/已失败的下游阶段标为待重新确认，并记录集级过期详情。

        只改 `status` 与 `stage_progress`（UI 元数据）：MUST NOT 触碰任何阶段的产物内容、
        `selected`/`versions`，也 MUST NOT 删磁盘文件。

        `baseline` MUST 是**本次重算之前**的阶段状态快照：`_recalculate_all_statuses` 会把
        `stopped`/`waiting` 按产物完整性算成 `completed`，若拿重算后的值作判据会把本该保持
        原状的阶段（尤其用户停住的 `stopped`）误标为待重新确认。标记同时要求“变更前已执行且
        重算后仍为已执行”：`completed` → 重算为 `waiting`（条目被同步重置）的阶段不抢标记，
        交给 `waiting` 自己表达“缺产物”；`running`/`pending`/`stopped` 不参不标。

        详情取并集写入：多次保存（如先改正文再改角色）MUST NOT 把已记录的过期集冲掉。
        """
        changed = False
        markable = ("completed", "session_completed", "error")
        pending_action = ("waiting", STATUS_RECONFIRM)
        incoming = {int(n) for n in (stale_episodes or [])}
        for key in stage_keys:
            before = (baseline or {}).get(key, state.status.get(key))
            now = state.status.get(key)
            mark = before in markable and now in markable
            if mark:
                state.status[key] = STATUS_RECONFIRM
                logger.info("[reconfirm] %s 因剧本结构变更标为待重新确认（原 %s）", key, before)
            elif before not in pending_action and now not in pending_action:
                continue
            existing = state.stage_progress.get(key)
            progress = dict(existing) if isinstance(existing, dict) else {}
            merged = incoming | {n for n in (progress.get("stale_episodes") or []) if isinstance(n, int)}
            progress["stale_episodes"] = sorted(merged)
            progress["stale_reason"] = STALE_REASON_SCRIPT
            progress["stale_at"] = time.time()
            state.stage_progress[key] = progress
            changed = True
        return changed

    def _mark_final_videos_stale(self, state: WorkflowState) -> bool:
        """成片只提示不失效：已有 final_videos 时写 stale 详情，不改阶段状态。"""
        key = WorkflowStage.POST_PRODUCTION.value
        artifact = state.artifacts.get(key)
        final_videos = artifact.get("final_videos") if isinstance(artifact, dict) else None
        if not final_videos:
            return False
        existing = state.stage_progress.get(key)
        progress = dict(existing) if isinstance(existing, dict) else {}
        progress["stale"] = True
        progress["stale_reason"] = STALE_REASON_SCRIPT
        progress["stale_at"] = time.time()
        state.stage_progress[key] = progress
        return True

    def _sync_script_structure(
        self, state: WorkflowState, before: Optional[Dict[str, Any]], *,
        force_cast_sync: bool = False,
        baseline: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """剧本结构变更的统一收尾：重建阶段 2 条目 + 分级标记下游 + 成片提示。

        应用修订与手动编辑 MUST 走本入口，两条路径的判定与标记口径完全一致。返回可直接
        并入响应的过期信息（未发生变更时为空 dict）。

        调用点 MUST 排在 `_recalculate_all_statuses` 之后：否则本方法写的标记会立即被
        “产物完整即 completed”的重算冲回。`before` 为 None（尚无剧本产物）时不动作：
        首次写入不存“相对旧输入已变”的语义。
        """
        after = state.artifacts.get(WorkflowStage.SCRIPT_GENERATION.value)
        if not isinstance(after, dict) or before is None:
            return {}

        changed_cast = script_revision.structure_fingerprint(before, scope="cast") != \
            script_revision.structure_fingerprint(after, scope="cast")
        changed_body = script_revision.structure_fingerprint(before, scope="body") != \
            script_revision.structure_fingerprint(after, scope="body")
        if changed_cast or force_cast_sync:
            # 1) 同步：阶段 2 条目集合按新剧本重建（无产物时不创建，由种子化负责）。
            # force_cast_sync：请求体带了 characters/settings 时即使内容未变也跑一次，
            # 用于给旧会话的缺主键条目补回同源主键（只补键、不标下游）。
            self._rebuild_stage_items_from_script(state)
        if not (changed_cast or changed_body):
            return {}
        # 2) 先取重算前的状态基线（调用方传入的更能代表“变更前现场”：`stopped`/`waiting` 会
        # 被上游的重算算成 `completed`，拿它作判据会误标），再重算：新增 pending 条目会让
        # 阶段 2 自然变 waiting（不标 reconfirm）
        baseline = dict(baseline or state.status)
        self._recalculate_all_statuses(state)
        # 3) 标记：角色/场景变更从阶段 2 起；只改正文从分镜起（条目集合未变，阶段 2 不标）
        from_stage = (
            WorkflowStage.CHARACTER_DESIGN
            if changed_cast
            else WorkflowStage.STORYBOARD
        )
        changed_episodes = script_revision.diff_episode_numbers(
            before.get("episodes"), after.get("episodes")
        )
        stale_storyboards = script_revision.stale_storyboards(
            changed_episodes, state.artifacts.get(WorkflowStage.STORYBOARD.value)
        )
        self._mark_downstream_reconfirm(
            state,
            self._reconfirm_stage_keys(from_stage=from_stage),
            stale_storyboards if changed_body else [],
            baseline=baseline,
        )
        # 4) 成片：只写提示，不改状态、不清空、不重跑
        self._mark_final_videos_stale(state)
        # 5) 归位停点：stop_session 按 current_stage 找运行阶段，有 running 时 MUST 保持原值
        if not any(value == "running" for value in state.status.values()):
            state.current_stage = WorkflowStage.SCRIPT_GENERATION

        post_art = state.artifacts.get(WorkflowStage.POST_PRODUCTION.value)
        return {
            "stale_storyboards": stale_storyboards,
            "stale_final_videos": bool(
                isinstance(post_art, dict) and post_art.get("final_videos")
            ),
        }

    @staticmethod
    def _rerun_episode_numbers(intervention: Optional[Dict[str, Any]]) -> Set[int]:
        """分镜显式重跑的集号集合（非法值忽略）。"""
        if not isinstance(intervention, dict):
            return set()
        values = intervention.get("regenerate_episodes")
        if not isinstance(values, list):
            return set()
        numbers: Set[int] = set()
        for value in values:
            try:
                numbers.add(int(value))
            except (TypeError, ValueError):
                continue
        return numbers

    @staticmethod
    def _storyboard_segments_by_episode(artifact: Any) -> Dict[int, Set[str]]:
        """分镜产物 → {episode_number: {segment_id}}。"""
        result: Dict[int, Set[str]] = {}
        if not isinstance(artifact, dict):
            return result
        for episode in artifact.get("episodes") or []:
            if not isinstance(episode, dict):
                continue
            try:
                number = int(episode.get("episode_number"))
            except (TypeError, ValueError):
                continue
            ids = {
                str(seg.get("segment_id"))
                for seg in (episode.get("segments") or [])
                if isinstance(seg, dict) and seg.get("segment_id")
            }
            result.setdefault(number, set()).update(ids)
        return result

    def _prune_downstream_items_for_episodes(
        self,
        state: WorkflowState,
        episodes: Set[int],
        previous_storyboard: Any,
        payload: Any,
        *,
        baseline: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """分镜重跑后的下游收尾：移除孤儿条目引用 + 标待重新确认 + 成片提示。

        只移除产物中的引用（参考图 scenes / 改写 items / 视频 clips），MUST NOT 删磁盘文件；
        仍存在 segment 的 `selected`/`versions` 原样保留。孤儿判定以“重跑集的旧 segment
        − 新分镜全集”为准，因此条目缺 `episode` 字段时同样能按 segment_id 命中。
        """
        if not isinstance(payload, dict):
            # 拿不到新分镜结果时不能“全集都算孤儿”：宁可本次不清理，也不能误删条目引用
            logger.warning("[reconfirm] 分镜重跑收尾跳过：新产物形状非法（%r）", type(payload).__name__)
            return {"orphan_ids": [], "removed": {}}
        old_by_ep = self._storyboard_segments_by_episode(previous_storyboard)
        new_by_ep = self._storyboard_segments_by_episode(payload)
        # 只信已有产物的集：把不存在的集号写进过期详情会误导用户
        known_episodes = set(old_by_ep) | set(new_by_ep)
        episodes = episodes & known_episodes
        all_new_ids: Set[str] = {seg_id for ids in new_by_ep.values() for seg_id in ids}
        orphan_ids: Set[str] = set()
        for number in episodes:
            orphan_ids |= old_by_ep.get(number, set()) - all_new_ids

        removed: Dict[str, int] = {}
        if orphan_ids:
            containers = (
                (WorkflowStage.REFERENCE_GENERATION, "scenes"),
                (WorkflowStage.PROMPT_REWRITE, "items"),
                (WorkflowStage.VIDEO_GENERATION, "clips"),
            )
            for stage, list_key in containers:
                # 提示词改写被禁用时：不创建该键，也不参与清理
                if stage not in get_stage_order():
                    continue
                key = stage.value
                artifact = state.artifacts.get(key)
                items = artifact.get(list_key) if isinstance(artifact, dict) else None
                if not isinstance(items, list):
                    continue
                kept = [
                    item for item in items
                    if not (isinstance(item, dict) and str(item.get("id") or "") in orphan_ids)
                ]
                if len(kept) != len(items):
                    removed[key] = len(items) - len(kept)
                    artifact[list_key] = kept
                    state.artifacts[key] = artifact

        self._mark_downstream_reconfirm(
            state,
            self._reconfirm_stage_keys(from_stage=WorkflowStage.REFERENCE_GENERATION),
            sorted(episodes),
            baseline=baseline,
        )
        self._mark_final_videos_stale(state)
        if removed:
            logger.info("[reconfirm] 分镜重跑集 %s 清理失效条目：%s（磁盘文件保留）", sorted(episodes), removed)
        return {"orphan_ids": sorted(orphan_ids), "removed": removed}

    def persist_session_snapshot(self, session_id: str) -> Dict[str, Any]:
        """Return the latest committed status map (every change is already committed when made)."""
        state = self.get_state(session_id)
        if not state:
            raise KeyError(f"Session not found: {session_id}")
        # 仅取 status：跳过 needs_regeneration 派生；需要 artifacts 的调用方须显式 annotate=True
        return copy.deepcopy(self._view_dict(state, annotate=False)["status"])

    def stop_session(self, session_id: str):
        self.get_stop_event(session_id).set()

        def apply(after: WorkflowState) -> Any:
            stage = after.current_stage.value
            if after.status.get(stage) != "running":
                return _NO_COMMIT
            after.status[stage] = "stopped"
            after.error = None  # 清除错误，因为是主动停止
            current_progress = after.stage_progress.get(stage, {})
            after.stage_progress[stage] = {
                **current_progress,
                "step": "已停止",
                "message": "已停止",
                "updated_at": time.time(),
            }
            return None

        try:
            self._mutate(session_id, apply)
        except KeyError:
            pass
        with self._state_lock:
            self._errors.pop(session_id, None)
        logger.info(f"Session {session_id} stop signal sent")

    def reset_stop_event(self, session_id: str):
        with self._state_lock:
            if session_id in self._stop_events:
                self._stop_events[session_id].clear()

    def track_background_task(self, task: Any):
        """Keep detached workflow tasks alive after an SSE client disconnects."""
        with self._state_lock:
            self._background_tasks.add(task)

        def _cleanup(done_task: Any):
            with self._state_lock:
                self._background_tasks.discard(done_task)
            try:
                done_task.exception()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Detached workflow task failed")

        task.add_done_callback(_cleanup)

    def _get_next_stage(self, current: WorkflowStage) -> Optional[WorkflowStage]:
        order = get_stage_order()
        try:
            idx = order.index(current)
            if idx + 1 < len(order):
                return order[idx + 1]
            return None
        except ValueError:
            # current 不在启用序列（可选阶段被开关移除，如 prompt_rewrite 停点后禁用）：
            # 按固定全序回退到下一个仍启用的阶段，避免会话恢复时死锁
            full_idx = _FULL_STAGE_ORDER.index(current)
            for stage in _FULL_STAGE_ORDER[full_idx + 1:]:
                if stage in order:
                    logger.warning(
                        "阶段 %s 已被禁用（不在启用序列），继续时跳过至 %s",
                        current.value, stage.value,
                    )
                    return stage
            return None

    # ──────────── 跨阶段同步逻辑 ────────────
    def _sync_artifacts_cross_stages(self, state: WorkflowState, stage: WorkflowStage, payload: Dict):
        """
        跨阶段数据同步钩子：当某个阶段产生新数据时，自动推送到后续阶段。
        例如：剧本续写产生的新角色/分镜，自动同步到 Stage 2 和 Stage 3。
        """
        if not isinstance(payload, dict):
            return

        # 案例 1: 剧本续写确认 (Script Confirmation)
        if stage == WorkflowStage.SCRIPT_GENERATION:
            # 获取合并后的角色、场景和剧集
            new_chars = payload.get("new_characters", [])
            new_settings = payload.get("new_settings", [])
            new_episodes = payload.get("new_episodes", [])
            # 如果没有新增剧集数据，无需同步到 Stage 2 和 3
            if not new_episodes: 
                return 
            
            # (A) 同步到第二阶段 (角色设计)
            if new_chars or new_settings:
                # 注意：WorkflowStage.CHARACTER_DESIGN 是 Enum，这里需要使用 .value
                char_stage_key = WorkflowStage.CHARACTER_DESIGN.value
                char_art = state.artifacts.get(char_stage_key)
                if not isinstance(char_art, dict):
                    char_art = {"characters": [], "settings": [], "version": 1}
                
                existing_chars = char_art.get("characters", [])
                for nc in new_chars:
                    if not any(c.get("id") == nc.get("character_id") for c in existing_chars):
                        existing_chars.append({
                            "id": nc.get("character_id"), "name": nc.get("name"), "description": nc.get("description"),
                            "selected": "", "versions": []
                        })
                char_art["characters"] = existing_chars
                
                existing_sets = char_art.get("settings", [])
                for ns in new_settings:
                    if not any(s.get("id") == ns.get("setting_id") for s in existing_sets):
                        existing_sets.append({
                            "id": ns.get("setting_id"), "name": ns.get("name"), "description": ns.get("description"),
                            "selected": "", "versions": []
                        })
                char_art["settings"] = existing_sets
                state.artifacts[char_stage_key] = char_art

            # (B) 同步到第三阶段 (分镜设计)
            if new_episodes:
                story_stage_key = WorkflowStage.STORYBOARD.value
                story_art = state.artifacts.get(story_stage_key)
                if not isinstance(story_art, dict):
                    story_art = {"episodes": [], "version": 1}
                
                existing_eps = story_art.get("episodes", [])
                for ne in new_episodes:
                    ep_num = ne.get("episode_number")
                    if not any(e.get("episode_number") == ep_num for e in existing_eps):
                        existing_eps.append({
                            "episode_number": ep_num,
                            "episode_title": ne.get("act_title") or f"第{ep_num}集",
                            "segments": []
                        })
                existing_eps.sort(key=lambda x: x.get("episode_number", 0))
                story_art["episodes"] = existing_eps
                state.artifacts[story_stage_key] = story_art

        # 案例 2: 分镜生成或修改同步到第四、第五阶段 (Storyboard -> Ref/Video)
        if stage == WorkflowStage.STORYBOARD:
            episodes = payload.get("episodes", []) if isinstance(payload, dict) else payload
            if not isinstance(episodes, list):
                return
                
            all_sync_clips = []
            for ep in episodes:
                if not isinstance(ep, dict):
                    continue
                ep_n = ep.get("episode_number", 0)
                for s_i, seg in enumerate(ep.get("segments", []), 1):
                    if not isinstance(seg, dict):
                        continue
                    seg_id = seg.get("segment_id", f"seg_{ep_n:02d}_{s_i:02d}")
                    
                    # 汇总 segment 级别的描述和时长
                    shots = seg.get("shots", [])
                    desc_video = " ".join([sh.get("plot") or sh.get("content") or "" for sh in shots]).strip()
                    desc_ref = " ".join([sh.get("visual_prompt") or sh.get("plot") or sh.get("content") or "" for sh in shots]).strip()
                    total_dur = seg.get("total_duration") or sum([sh.get("duration", 0) for sh in shots]) or 10
                    
                    all_sync_clips.append({
                        "segment_id": seg_id,
                        "desc_video": desc_video,
                        "desc_ref": desc_ref,
                        "duration": total_dur,
                        "episode": ep_n,
                        "index": s_i,
                        "name": f"第{ep_n}集-片段{s_i}"
                    })
            
            if not all_sync_clips:
                return

            # (A) 同步到第四阶段 (参考图生成)
            ref_stage_key = WorkflowStage.REFERENCE_GENERATION.value
            ref_art = state.artifacts.get(ref_stage_key)
            if not isinstance(ref_art, dict):
                ref_art = {"scenes": [], "version": 1}
            
            existing_scenes = ref_art.get("scenes", [])
            for c_info in all_sync_clips:
                id = c_info["segment_id"]
                idx = next((i for i, s in enumerate(existing_scenes) if s.get("id") == id), -1)
                if idx == -1:
                    existing_scenes.append({
                        "id": id,
                        "name": c_info["name"],
                        "index": c_info["index"],
                        "description": c_info["desc_ref"],
                        "selected": "",
                        "versions": [],
                        "status": "pending",
                        "episode": c_info["episode"]
                    })
                else:
                    # 更新已有记录
                    existing_scenes[idx]["description"] = c_info["desc_ref"]
                    existing_scenes[idx]["episode"] = c_info["episode"]
                    existing_scenes[idx]["index"] = c_info["index"]
                    existing_scenes[idx]["name"] = c_info["name"]
            
            # 排序：确保片段显示顺序正确 (按 id 排序，例如 seg_01_01 < seg_07_01)
            existing_scenes.sort(key=lambda x: x.get("id", ""))
            ref_art["scenes"] = existing_scenes
            state.artifacts[ref_stage_key] = ref_art

            # (B) 同步到第五阶段 (视频生成)
            video_stage_key = WorkflowStage.VIDEO_GENERATION.value
            video_art = state.artifacts.get(video_stage_key)
            if not isinstance(video_art, dict):
                video_art = {"clips": [], "version": 1}
            
            existing_clips = video_art.get("clips", [])
            for c_info in all_sync_clips:
                id = c_info["segment_id"]
                idx = next((i for i, c in enumerate(existing_clips) if c.get("id") == id), -1)
                if idx == -1:
                    existing_clips.append({
                        "id": id,
                        "name": c_info["name"],
                        "index": c_info["index"],
                        "description": c_info["desc_video"],
                        "duration": c_info["duration"],
                        "selected": "",
                        "versions": [],
                        "status": "pending",
                        "episode": c_info["episode"]
                    })
                else:
                    existing_clips[idx]["description"] = c_info["desc_video"]
                    existing_clips[idx]["duration"] = c_info["duration"]
                    existing_clips[idx]["episode"] = c_info["episode"]
                    existing_clips[idx]["index"] = c_info["index"]
                    existing_clips[idx]["name"] = c_info["name"]
            
            # 排序：确保片段显示顺序正确
            existing_clips.sort(key=lambda x: x.get("id", ""))
            video_art["clips"] = existing_clips
            state.artifacts[video_stage_key] = video_art

            # (C) 同步到提示词改写阶段（prompt_rewrite）：分镜内容变更后改写结果过期，
            # 已完成的改写条目置为 pending，提示用户重新生成
            if WorkflowStage.PROMPT_REWRITE in get_stage_order():
                pr_key = WorkflowStage.PROMPT_REWRITE.value
                pr_art = state.artifacts.get(pr_key)
                if isinstance(pr_art, dict) and isinstance(pr_art.get("items"), list):
                    synced_ids = {c["segment_id"] for c in all_sync_clips}
                    for item in pr_art["items"]:
                        if isinstance(item, dict) and item.get("id") in synced_ids and item.get("status") == "done":
                            item["status"] = "pending"
                    state.artifacts[pr_key] = pr_art

        # 案例 3: 角色/场景描述修改同步回剧本元数据，保证后续阶段读到用户最新描述。
        if stage == WorkflowStage.CHARACTER_DESIGN:
            script_art = state.artifacts.get(WorkflowStage.SCRIPT_GENERATION.value)
            if not isinstance(script_art, dict):
                return

            char_by_id = {
                c.get("id"): c for c in payload.get("characters", [])
                if isinstance(c, dict) and c.get("id")
            }
            setting_by_id = {
                s.get("id"): s for s in payload.get("settings", [])
                if isinstance(s, dict) and s.get("id")
            }

            for char in script_art.get("characters", []):
                if not isinstance(char, dict):
                    continue
                # 与种子化/取数同源：剧本条目缺主键时按名称派生，否则描述永远回写不到剧本
                source = char_by_id.get(script_revision.item_id(char, "character_id", "char"))
                if source:
                    for key in ("name", "description", "species"):
                        if source.get(key):
                            char[key] = source[key]

            for setting in script_art.get("settings", []):
                if not isinstance(setting, dict):
                    continue
                source = setting_by_id.get(script_revision.item_id(setting, "setting_id", "set"))
                if source:
                    for key in ("name", "description"):
                        if source.get(key):
                            setting[key] = source[key]

    def _recalculate_all_statuses(self, state: WorkflowState):
        """
        根据各阶段 artifacts 的数据完整性重新计算 status 字典。
        逻辑：
        - 如果 artifacts[stage] 不存在: pending
        - 如果存在且包含核心列表(characters/scenes/clips等):
            - 如果列表项存在 selected 为空的情况: waiting
            - 如果列表项全部已选择(或不需要选择): completed
        """
        for stage in WorkflowStage:
            if stage in [WorkflowStage.INIT, WorkflowStage.COMPLETED]:
                continue
            # 可选阶段被开关禁用时（不在启用序列）不参与重算：状态冻结，
            # 重新启用后恢复；避免禁用后历史停点状态被打回 pending 导致会话无法继续
            if stage not in get_stage_order():
                continue
            s_val = stage.value
            # 如果当前阶段正在运行，不自动覆盖其为 completed/waiting (除非它目前是空)
            # reconfirm 同等对待：“产物已完成但输入已变”是显式标记，不得被任何写入的重算冲回
            current_s_status = state.status.get(s_val, "pending")
            if current_s_status in ("running", "error", STATUS_RECONFIRM):
                continue

            art = state.artifacts.get(s_val)
            if not art or not isinstance(art, dict):
                state.status[s_val] = "pending"
                continue
            
            # 检查是否有待处理的“空数据占位”
            has_pending = False
            
            if s_val == "character_design":
                chars = art.get("characters", [])
                sets = art.get("settings", [])
                if any(not c.get("selected") for c in chars) or any(not s.get("selected") for s in sets):
                    has_pending = True
            elif s_val == "storyboard":
                # 检查分镜阶段：如果存在剧集（episode）但其 segments 为空，视为 waiting
                episodes = art.get("episodes", [])
                if not episodes or any(not ep.get("segments") for ep in episodes):
                    has_pending = True
            elif s_val == "reference_generation":
                scenes = art.get("scenes", [])
                mode = str((state.meta or {}).get("video_generation_mode") or "first_frame") if isinstance(state.meta, dict) else "first_frame"
                if mode == "reference":
                    # reference 模式：产物不参与下游取值，一律判完成，不读清单内容
                    # （占位条目由分镜同步写入，不得被读作待生成）
                    has_pending = False
                elif not scenes or any(not s.get("selected") for s in scenes):
                    has_pending = True
            elif s_val == "prompt_rewrite":
                items = art.get("items", [])
                if not items or any(i.get("status") not in ("done", "failed") for i in items):
                    has_pending = True
            elif s_val == "video_generation":
                clips = art.get("clips", [])
                if not clips or any(not c.get("selected") for c in clips):
                    has_pending = True
            
            if has_pending:
                state.status[s_val] = "waiting"
            else:
                # 已经有数据且没有 pending 项，标记为完成
                state.status[s_val] = "completed"

    @staticmethod
    def _preserve_item_error_fields(previous_artifact: Dict[str, Any], payload: Dict[str, Any], stage: WorkflowStage) -> None:
        """阶段完成时保留失败条目的 error_type/error（payload 未携带时从上一版产物覆盖）。"""
        item_keys_by_stage = {
            WorkflowStage.CHARACTER_DESIGN: ("characters", "settings"),
            WorkflowStage.REFERENCE_GENERATION: ("scenes",),
            WorkflowStage.PROMPT_REWRITE: ("items",),
            WorkflowStage.VIDEO_GENERATION: ("clips",),
        }
        keys = item_keys_by_stage.get(stage, ())
        if not keys or not isinstance(previous_artifact, dict) or not isinstance(payload, dict):
            return
        for key in keys:
            prev_items = previous_artifact.get(key)
            new_items = payload.get(key)
            if not isinstance(prev_items, list) or not isinstance(new_items, list):
                continue
            prev_by_id = {
                item.get("id"): item
                for item in prev_items
                if isinstance(item, dict) and item.get("id")
            }
            for item in new_items:
                if not isinstance(item, dict):
                    continue
                prev = prev_by_id.get(item.get("id"))
                if not prev or item.get("status") != "failed":
                    continue
                if prev.get("error_type") and not item.get("error_type"):
                    item["error_type"] = prev["error_type"]
                if prev.get("error") and not item.get("error"):
                    item["error"] = prev["error"]

    @staticmethod
    def _is_background_item_regeneration(stage: WorkflowStage, intervention: Optional[Dict]) -> bool:
        if not isinstance(intervention, dict):
            return False
        if stage == WorkflowStage.CHARACTER_DESIGN:
            return isinstance(intervention.get("regenerate_characters"), list) or isinstance(intervention.get("regenerate_settings"), list)
        if stage == WorkflowStage.REFERENCE_GENERATION:
            return isinstance(intervention.get("regenerate_scenes"), list)
        if stage == WorkflowStage.PROMPT_REWRITE:
            # revise_items 只返回被修订条目：必须走条目合并，否则整阶段产物被部分列表覆盖，
            # 其余分镜提示词会丢失；前端 isBackgroundItemRegeneration 也是同一口径。
            return isinstance(intervention.get("regenerate_items"), list) or isinstance(
                intervention.get("revise_items"), list
            )
        if stage == WorkflowStage.VIDEO_GENERATION:
            return isinstance(intervention.get("regenerate_clips"), list)
        if stage == WorkflowStage.POST_PRODUCTION:
            return isinstance(intervention.get("regenerate_episodes"), list)
        return False

    @staticmethod
    def _background_regeneration_targets(stage: WorkflowStage, intervention: Optional[Dict]) -> Dict[str, Set[str]]:
        if not isinstance(intervention, dict):
            return {}
        if stage == WorkflowStage.CHARACTER_DESIGN:
            return {
                "characters": set(intervention.get("regenerate_characters") or []),
                "settings": set(intervention.get("regenerate_settings") or []),
            }
        if stage == WorkflowStage.REFERENCE_GENERATION:
            return {"scenes": set(intervention.get("regenerate_scenes") or [])}
        if stage == WorkflowStage.PROMPT_REWRITE:
            revise_ids = {
                str(item.get("id"))
                for item in (intervention.get("revise_items") or [])
                if isinstance(item, dict) and item.get("id")
            }
            return {"items": set(intervention.get("regenerate_items") or []) | revise_ids}
        if stage == WorkflowStage.VIDEO_GENERATION:
            return {"clips": set(intervention.get("regenerate_clips") or [])}
        return {}

    @staticmethod
    def _merge_item_regeneration_payload(
        existing: Any,
        payload: Any,
        item_keys: List[str],
        target_ids_by_key: Optional[Dict[str, Set[str]]] = None,
        baseline_fields: Optional[Dict[str, Any]] = None,
        protected_fields: tuple = (),
    ) -> Dict:
        """Merge concurrent single-item regeneration results without clobbering fresher items.

        `baseline_fields` 为执行开始时的字段快照（`{container: {id: {field: value}}}`）。
        目标条目里：
        - `selected` 由 `_selected_after_asset_update` 判定（Q8）；
        - `protected_fields`（A1b，按阶段声明）若在执行期间被改过，则保留用户值；
          本轮送空字符串也视为“本轮没有该值”，同样回退当前值。
        """
        if not isinstance(existing, dict):
            existing = {}
        if not isinstance(payload, dict):
            return copy.deepcopy(existing)

        merged = copy.deepcopy(existing)
        baseline_map: Dict[str, Any] = baseline_fields or {}
        for key, value in payload.items():
            if key not in item_keys:
                merged[key] = copy.deepcopy(value)

        for key in item_keys:
            existing_items = merged.get(key, [])
            payload_items = payload.get(key, [])
            if not isinstance(existing_items, list):
                existing_items = []
            if not isinstance(payload_items, list):
                merged[key] = existing_items
                continue

            target_ids = (target_ids_by_key or {}).get(key)
            by_id = {
                item.get("id"): copy.deepcopy(item)
                for item in existing_items
                if isinstance(item, dict) and item.get("id")
            }
            order = [
                item.get("id")
                for item in existing_items
                if isinstance(item, dict) and item.get("id")
            ]

            for item in payload_items:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                item_id = item["id"]
                if target_ids is not None and item_id not in target_ids:
                    continue
                current = by_id.get(item_id)
                if item_id not in order:
                    order.append(item_id)
                if not current:
                    by_id[item_id] = copy.deepcopy(item)
                    continue
                current_versions = current.get("versions") if isinstance(current.get("versions"), list) else []
                item_versions = item.get("versions") if isinstance(item.get("versions"), list) else []
                merged_versions = WorkflowEngine._merge_asset_versions(current_versions, item_versions)
                base_item = (baseline_map.get(key) or {}).get(item_id) or {}
                if len(item_versions) > len(current_versions):
                    merged_item = {**current, **copy.deepcopy(item)}
                    merged_item["versions"] = merged_versions
                    merged_item["selected"] = WorkflowEngine._selected_after_asset_update(
                        current.get("selected"), item.get("selected"),
                        base_item.get("selected", _NO_BASELINE),
                    )
                    WorkflowEngine._apply_protected_fields(
                        merged_item, current, item, base_item, protected_fields
                    )
                    if merged_item.get("selected") and item.get("status") in {"done", "failed"}:
                        merged_item["status"] = "done"
                    by_id[item_id] = merged_item
                elif len(item_versions) == len(current_versions):
                    merged_item = {**current, **copy.deepcopy(item)}
                    merged_item["selected"] = WorkflowEngine._selected_after_asset_update(
                        current.get("selected"), item.get("selected"),
                        base_item.get("selected", _NO_BASELINE),
                    )
                    WorkflowEngine._apply_protected_fields(
                        merged_item, current, item, base_item, protected_fields
                    )
                    if merged_item.get("selected") and item.get("status") in {"done", "failed"}:
                        merged_item["status"] = "done"
                    if merged_versions:
                        merged_item["versions"] = merged_versions
                    if (
                        current.get("status") == "failed"
                        and item.get("status") == "done"
                        and not current.get("selected")
                    ):
                        merged_item["status"] = "failed"
                    by_id[item_id] = merged_item

            merged[key] = [by_id[item_id] for item_id in order if item_id in by_id]
        return merged

    @staticmethod
    def _merge_asset_versions(current_versions: Any, new_versions: Any) -> List[str]:
        merged: List[str] = []
        for value in list(current_versions if isinstance(current_versions, list) else []) + list(new_versions if isinstance(new_versions, list) else []):
            if value and value not in merged:
                merged.append(value)
        return merged

    @staticmethod
    def _selected_after_asset_update(current_selected: Any, new_selected: Any,
                                     baseline_selected: Any = _NO_BASELINE) -> Any:
        """条目的有效选中（Q8）。

        给了基线且当前值已偏离基线 → 视为“执行期间用户改过”，保留当前值；
        否则显式重生成的新结果生效（旧值仅在本次为空时回退）。
        未给基线时无法判定手改，按“本次优先”处理。
        """
        if baseline_selected is not _NO_BASELINE and current_selected != baseline_selected:
            return current_selected
        return new_selected or current_selected or ""

    @staticmethod
    def _field_edited_by_user(current: Any, base: Any, field: str) -> bool:
        """`current[field]` 是否偏离基线（Q8/A1b/全量路径共用的比较口径）。

        仅回答“两边都存在且不同”；基线缺字段算不算手改由调用方决定：
        后台保护视作“未改”（payload 生效），全量路径保留其原有“基线缺 = 改过”语义。
        """
        if not isinstance(current, dict) or field not in current:
            return False
        if not isinstance(base, dict) or field not in base:
            return False
        return current.get(field) != base.get(field)

    @staticmethod
    def _apply_protected_fields(merged_item: Dict[str, Any], current: Dict[str, Any],
                                item: Dict[str, Any], base_item: Dict[str, Any],
                                protected_fields: tuple) -> None:
        """A1b：执行期间被改过的受保护字段保留用户值；本轮送空也回退当前值。

        `selected` 不在此处理（由 `_selected_after_asset_update` 单独管）。
        """
        for field in protected_fields or ():
            if field not in current:
                continue
            edited = WorkflowEngine._field_edited_by_user(current, base_item, field)
            incoming = item.get(field)
            if edited or incoming in (None, ""):
                merged_item[field] = copy.deepcopy(current.get(field))

    def _stage_baseline(self, run: "_StageRun") -> Dict[str, Any]:
        """本次执行的字段基线：同一执行内只算一次，所有事件与收尾看到同一份。"""
        if run.baseline_fields is None:
            run.baseline_fields = self._user_edit_snapshot(run.start_artifacts.get(run.stage.value))
        return run.baseline_fields

    @staticmethod
    def _user_edit_snapshot(artifact: Any) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """执行开始时用户可编辑字段的快照：`{container: {id: {field: value}}}`。

        Q8（选中）与 A1b（字段级）共用同一结构，避免“选中基线来自 A、字段基线来自 B”。
        """
        snapshot: Dict[str, Dict[str, Dict[str, Any]]] = {}
        if not isinstance(artifact, dict):
            return snapshot
        for container, items in artifact.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                record = {
                    field: copy.deepcopy(item[field])
                    for field in _USER_EDIT_FIELDS
                    if field in item
                }
                if record:
                    snapshot.setdefault(str(container), {})[item["id"]] = record
        return snapshot

    async def execute_stage(self,
                            state: WorkflowState,
                            stage: WorkflowStage,
                            input_data: Any,
                            cancellation_check: Optional[Callable] = None,
                            progress_callback: Optional[Callable] = None,
                            intervention: Optional[Dict] = None) -> Dict:
        """执行一个阶段：开始提交 → 按 D14 物化输入 → Agent 写执行工作根 → 发布 → 结束提交。

        所有 DB/MinIO I/O 都不持有全局 _state_lock；事件中的媒体路径在提交后才以 public_path 发出。
        """
        if not isinstance(input_data, dict):
            input_data = {}
        else:
            input_data = copy.deepcopy(input_data)

        sid = state.session_id
        run = _StageRun(
            session_id=sid,
            stage=stage,
            execution_id=new_execution_id(),
            background=self._is_background_item_regeneration(stage, intervention),
            intervention=intervention,
        )
        session_stop = self.get_stop_event(sid)

        def combined_cancel_check():
            return session_stop.is_set() or (cancellation_check and cancellation_check())

        if not run.background:
            await asyncio.to_thread(self._begin_stage, run)
        inputs = None
        try:
            view = await asyncio.to_thread(self._require, sid)
            run.owner = view.owner
            run.start_artifacts = copy.deepcopy(view.artifacts)
            run.publisher = workflow_store.ExecutionPublisher(
                owner=view.owner, session_id=sid, stage=stage.value,
                execution_id=run.execution_id, boot_id=self._boot_id,
            )
            user = {"login": view.owner, "owner_key": media_refs.owner_key(view.owner)}
            history = self._version_history(stage.value, view)
            inputs = await asyncio.to_thread(self._materialize_inputs, run, view, input_data, user, history)
            run.publisher.runtime_map.update(inputs.runtime_map)

            input_data["_session_meta"] = copy.deepcopy(view.meta)
            input_data["_session_artifacts"] = inputs.runtime
            # 阶段执行必须携带会话归属：上传文档等用户输入的 owner 校验依赖它
            input_data["_session_owner"] = view.owner
            for name, value in inputs.explicit.items():
                input_data[name] = value
            self._inject_user_selections(inputs.runtime, stage.value, input_data)

            agent = self.agent_factories[stage]()
            agent.set_cancellation_check(combined_cancel_check)
            os.makedirs(run.publisher.work_root, exist_ok=True)
            agent.execution_root = run.publisher.work_root
            agent.version_history = history

            def wrapped_progress_callback(phase: str, step: str, percent: float, data: dict = None):
                self._on_progress(run, progress_callback, phase, step, percent, data)

            agent.set_progress_callback(wrapped_progress_callback)

            with call_context(stage=stage.value, session_id=sid, execution_id=run.execution_id):
                result = await agent.process(input_data, intervention=intervention)
            if not isinstance(result, dict):
                logger.error(f"[execute_stage] Agent {stage.value} returned non-dict result: {type(result)}")
                result = {"payload": result}
            payload = workflow_store.strip_internal(result.get("payload", {}))
            mapped, published = await asyncio.to_thread(self._publish_stage_payload, run, payload)
            final_state = await asyncio.to_thread(self._finish_stage, run, result, mapped, published)
            result = dict(result)
            result["payload"] = copy.deepcopy(final_state.artifacts.get(stage.value))
            logger.info(
                "[execute_stage] stage=%s intervention=%s requires_intervention=%s stage_completed=%s revision=%s",
                stage.value, intervention is not None, result.get("requires_intervention"),
                result.get("stage_completed"), final_state.revision,
            )
            return result

        except asyncio.CancelledError:
            self._fail_stage(run, None)
            raise

        except Exception as e:
            # 必需输入不可用时携带可定位清单（前端按条目标红并引导重新选择/重生成）
            as_dicts = getattr(e, "as_dicts", None)
            self._fail_stage(run, str(e), missing_inputs=as_dicts() if callable(as_dicts) else None)
            raise
        finally:
            if inputs is not None:
                inputs.release()
            self._release_staging(run)
            self._cleanup_work_root(run)
            self._clear_overlay(sid, stage.value)
            if run.active_registered:
                with self._state_lock:
                    self._active_sessions.discard(sid)

    # ──────────── 阶段执行辅助 ────────────

    def _begin_stage(self, run: "_StageRun") -> None:
        """登记执行权 → 提交 running 与执行层 start；任一步失败都释放执行权（R20）。"""
        sid, stage = run.session_id, run.stage
        with self._session_lock(sid):
            with self._state_lock:
                if sid in self._active_sessions:
                    raise RuntimeError(f"Session {sid} is already running a stage.")
                self._active_sessions.add(sid)
                run.active_registered = True
            try:
                before = self._require(sid)
                stale = before.executions.get(stage.value)
                if isinstance(stale, dict) and stale.get("status") == "running" and stale.get("execution_id"):
                    # 本进程内未收尾的旧执行（如上次结束提交失败）：先以 interrupted 结束
                    self._mutate(sid, lambda _after: None, execution_states={stage.value: {
                        "action": "finish", "execution_id": stale["execution_id"],
                        "status": "interrupted", "error": "新的执行开始前未正常收尾",
                    }})

                def start(after: WorkflowState) -> None:
                    after.current_stage = stage
                    after.status[stage.value] = "running"
                    # 归属凭据与“真正持有执行权”同生命周期（D6）
                    after.boot_id = self._boot_id
                    after.error = None
                    after.stage_progress[stage.value] = {
                        "phase": stage.value,
                        "step": "启动中...",
                        "message": "启动中...",
                        "percent": 0,
                        "updated_at": time.time(),
                    }

                self._mutate(sid, start, execution_states={stage.value: {
                    "action": "start", "execution_id": run.execution_id, "boot_id": self._boot_id,
                }})
                with self._state_lock:
                    self._errors.pop(sid, None)
            except Exception:
                with self._state_lock:
                    self._active_sessions.discard(sid)
                    run.active_registered = False
                raise

    def _version_history(self, stage: str, view: WorkflowState) -> Dict[str, List[str]]:
        """Agent 可见的历史版本：DB 有效视图中的 versions + 上次未合入的已发布候选（不扫描磁盘）。"""
        history: Dict[str, List[str]] = {}
        artifact = view.artifacts.get(stage)
        for container in workflow_store.ITEM_CONTAINERS.get(stage, ()):
            items = artifact.get(container) if isinstance(artifact, dict) else None
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                versions: List[str] = []
                for entry in item.get("versions") or []:
                    value = entry if isinstance(entry, str) else (entry.get("path") if isinstance(entry, dict) else None)
                    if isinstance(value, str) and value.strip() and value not in versions:
                        versions.append(value)
                history[workflow_store.execution_item_key(container, str(item["id"]))] = versions
        execution = view.executions.get(stage)
        if isinstance(execution, dict):
            for public, info in (execution.get("version_candidates") or {}).items():
                key = str((info or {}).get("item") or "")
                if key:
                    bucket = history.setdefault(key, [])
                    if public not in bucket:
                        bucket.append(public)
        return history

    def _materialize_inputs(
        self,
        run: "_StageRun",
        view: WorkflowState,
        input_data: Dict[str, Any],
        user: Dict[str, Any],
        history: Dict[str, List[str]],
    ):
        """完整快照注入后按阶段/模式/目标条目只物化实际消费的字段（运行副本）。"""
        from core.consumption import materialize_stage_inputs

        stage = run.stage
        intervention = run.intervention if isinstance(run.intervention, dict) else {}
        mode = str(input_data.get("video_generation_mode") or view.meta.get("video_generation_mode") or "first_frame")
        target_ids = None
        episodes = None
        if stage == WorkflowStage.VIDEO_GENERATION:
            if isinstance(intervention.get("regenerate_clips"), list):
                target_ids = [str(item) for item in intervention["regenerate_clips"]]
            elif not intervention.get("selected_clips"):
                # 全量生成只补还没有任何版本的片段（与 VideoDirectorAgent 的跳过规则一致）
                target_ids = [
                    key.split(":", 1)[1] for key in self._segment_keys(view)
                    if not history.get(key)
                ]
            else:
                target_ids = []
        elif stage == WorkflowStage.REFERENCE_GENERATION and isinstance(intervention.get("regenerate_scenes"), list):
            target_ids = [str(item) for item in intervention["regenerate_scenes"]]
        elif stage == WorkflowStage.PROMPT_REWRITE and isinstance(intervention.get("regenerate_items"), list):
            target_ids = [str(item) for item in intervention["regenerate_items"]]
        elif stage == WorkflowStage.POST_PRODUCTION and isinstance(intervention.get("regenerate_episodes"), list):
            episodes = [int(ep) for ep in intervention["regenerate_episodes"] if str(ep).isdigit()]
        explicit: Dict[str, Any] = {}
        if stage == WorkflowStage.VIDEO_GENERATION and str(input_data.get("audio_reference_url") or "").strip():
            explicit["audio_reference_url"] = str(input_data["audio_reference_url"]).strip()
        artifacts = {key: workflow_store.split_preview(value)[0] for key, value in view.artifacts.items()}
        return materialize_stage_inputs(
            stage.value,
            artifacts,
            user,
            mode=mode,
            target_ids=target_ids,
            episodes=episodes,
            explicit=explicit,
            reference_factory=self._reference_factory(run, user),
        )

    @staticmethod
    def _segment_keys(view: WorkflowState) -> List[str]:
        storyboard = view.artifacts.get(WorkflowStage.STORYBOARD.value)
        keys: List[str] = []
        for episode in (storyboard or {}).get("episodes") or [] if isinstance(storyboard, dict) else []:
            for segment in episode.get("segments") or [] if isinstance(episode, dict) else []:
                if isinstance(segment, dict) and segment.get("segment_id"):
                    keys.append(workflow_store.execution_item_key("clips", str(segment["segment_id"])))
        return keys

    def _reference_factory(self, run: "_StageRun", user: Dict[str, Any]) -> Callable[[str, Any], Any]:
        """为物化结果附加推理引用：受管对象→按需预签名；legacy 本地→按需受管暂存（延迟到适配器取候选）。"""
        from core.reference_values import build_media_reference, stage_for_http
        from models.media_types import MediaReference

        def factory(canonical: str, resolved: Any) -> Any:
            value = str(canonical)
            if not value.startswith(media_refs.PUBLIC_PREFIX):
                return None
            ref = build_media_reference(value, user)
            if ref.presign_factory is not None:
                return MediaReference(public_path=value, local_path=str(resolved), presign_factory=ref.presign_factory)
            cache: Dict[str, Any] = {}

            def staged_candidates() -> List[str]:
                if "ref" not in cache:
                    staged_ref, staged = stage_for_http(
                        value, user, domain="sessions", resource_id=run.session_id,
                        execution_id=run.execution_id, boot_id=self._boot_id,
                    )
                    cache["ref"] = staged_ref
                    if staged is not None:
                        run.staged.append(staged)
                return cache["ref"].candidates()

            return MediaReference(public_path=value, local_path=str(resolved), presign_factory=staged_candidates)

        return factory

    @staticmethod
    def _cleanup_work_root(run: "_StageRun") -> None:
        """执行终态后清理本次工作根（D17：仍有在途操作或 pin 时保留，由清理器逐文件处理）。"""
        from core.object_operations import cleanup_execution_root

        if run.publisher is None:
            return
        try:
            cleanup_execution_root(run.publisher.owner_key, "sessions", run.session_id, run.execution_id)
        except Exception:
            logger.warning("execution root cleanup failed: %s/%s", run.session_id, run.execution_id, exc_info=True)

    def _release_staging(self, run: "_StageRun") -> None:
        from core.reference_values import release_staging

        while run.staged:
            staged = run.staged.pop()
            try:
                release_staging(staged, remote_done=True)
            except Exception:
                logger.warning("staging release failed: %s", staged, exc_info=True)

    def _on_progress(self, run: "_StageRun", progress_callback: Optional[Callable],
                     phase: str, step: str, percent: float, data: Optional[dict]) -> None:
        """Agent 进度回调：进度/预览只进覆盖层；条目终态先发布+提交，再以 public_path 转发。"""
        sid, stage_key = run.session_id, run.stage.value
        try:
            safe_percent = max(0, min(100, int(round(float(percent)))))
        except (TypeError, ValueError):
            safe_percent = 0
        message = f"{phase}: {step}" if phase and step else (step or phase or "")
        progress = {"phase": phase, "step": step, "message": message, "percent": safe_percent, "updated_at": time.time()}
        overlay = self._overlay(sid, stage_key)
        with self._state_lock:
            overlay["progress"] = progress
            # 运行中覆盖层的基线：本轮执行开始时的已提交快照，预览与条目事件共用（design D2）。
            # 只在本阶段覆盖层首次触达时登记：每份预览重取基线会让"上一次预览之后的用户写入"
            # 因与基线相等而被判为"无变化"，遮蔽会重新出现。
            # 字段基线走 `_stage_baseline(run)`（同一执行内只算一次并缓存在 run 上），与后台重生成
            # 事件合入共用同一份，不重复计算。
            if overlay.get("base") is None:
                base = copy.deepcopy(run.start_artifacts.get(stage_key))
                overlay["base"] = base if isinstance(base, dict) else {}
                overlay["baseline_fields"] = self._stage_baseline(run)

        forwarded = data
        if isinstance(data, dict) and run.publisher is not None:
            forwarded = dict(data)
            if data.get("assets_preview") is not None:
                preview = workflow_store.map_media(stage_key, copy.deepcopy(data["assets_preview"]), run.publisher.display)
                with self._state_lock:
                    overlay["preview"] = workflow_store.strip_internal(preview)
                forwarded["assets_preview"] = preview
                if data.get("persist"):
                    try:
                        self._persist_partial(run, data["assets_preview"])
                    except Exception:
                        logger.warning("partial artifact persist failed: %s/%s", sid, stage_key, exc_info=True)
            asset = data.get("asset_complete")
            if isinstance(asset, dict):
                if asset.get("status") in ("done", "failed"):
                    try:
                        mapped = self._commit_item_event(run, asset)
                    except Exception:
                        logger.warning("item event commit failed: %s/%s/%s", sid, stage_key, asset.get("id"), exc_info=True)
                        mapped = workflow_store.map_item_media(stage_key, copy.deepcopy(asset), run.publisher.display)
                else:
                    mapped = workflow_store.map_item_media(stage_key, copy.deepcopy(asset), run.publisher.display)
                with self._state_lock:
                    overlay["items"][(str(mapped.get("type")), str(mapped.get("id")))] = mapped
                forwarded["asset_complete"] = mapped
        if progress_callback:
            progress_callback(phase, step, percent, forwarded)

    def _persist_partial(self, run: "_StageRun", preview: Any) -> None:
        """已完成的部分产物（如分镜逐集）写工作层，重启后可续；不伪造 completed。"""
        if not isinstance(preview, dict):
            return
        payload = workflow_store.strip_internal(copy.deepcopy(preview))
        with run.lock:
            def apply(after: WorkflowState) -> Any:
                if after.artifacts.get(run.stage.value) == payload:
                    return _NO_COMMIT
                after.artifacts[run.stage.value] = payload
                return None

            self._mutate(run.session_id, apply, publisher=run.publisher)

    def _commit_item_event(self, run: "_StageRun", asset: Dict[str, Any]) -> Dict[str, Any]:
        """单条终态：发布新媒体后提交（全量执行写执行层，后台重生成直接合入有效产物）。"""
        sid, stage_key = run.session_id, run.stage.value
        container, item_id = str(asset.get("type") or ""), str(asset.get("id") or "")
        with run.lock:
            view = self._require(sid)
            carried = workflow_store.carried_media(view.artifacts, view.executions)
            mapped_items, published = run.publisher.publish({stage_key: copy.deepcopy(asset)}, carried, item_mode=True)
            item = mapped_items[stage_key]
            if not container or not item_id:
                return item
            if run.background:
                # 本次执行的字段基线：同一执行内只算一次（Q8/A1b 共用）
                baseline_fields = self._stage_baseline(run)

                def apply(after: WorkflowState) -> None:
                    artifact = after.artifacts.get(stage_key)
                    artifact = copy.deepcopy(artifact) if isinstance(artifact, dict) else {}
                    self._merge_asset_update(artifact, item, baseline_fields)
                    after.artifacts[stage_key] = artifact
                    self._recalculate_all_statuses(after)

                self._mutate(sid, apply, published=published, publisher=run.publisher)
            else:
                key = workflow_store.execution_item_key(container, item_id)
                # `evaluation` 已由 Agent 收敛为白名单（Q9），随条目一起透传落库
                record = {k: copy.deepcopy(v) for k, v in item.items() if k != "type"}
                record["seq"] = run.next_seq(key)
                delta = {
                    "execution_id": run.execution_id,
                    "active_items": {key: record},
                    "version_candidates": {
                        public: {"item": key} for public in workflow_store.published_paths(published)
                    },
                }
                try:
                    self._mutate(sid, lambda _after: _NO_COMMIT, published=published,
                                 execution_deltas={stage_key: delta}, publisher=run.publisher)
                except CommitStale:
                    logger.warning("late item event ignored: %s/%s/%s", sid, stage_key, key)
            return item

    def _publish_stage_payload(self, run: "_StageRun", payload: Any) -> tuple:
        """阶段结果发布（锁外上传）：已在视图中的旧引用原样保留，工作根新文件上传为不可变对象。"""
        stage_key = run.stage.value
        with run.lock:
            view = self._require(run.session_id)
            carried = workflow_store.carried_media(view.artifacts, view.executions)
            mapped, published = run.publisher.publish({stage_key: payload}, carried)
        return mapped[stage_key], published

    def _finish_stage(self, run: "_StageRun", result: Dict[str, Any], payload: Any,
                      published: Optional[Dict[str, Any]]) -> WorkflowState:
        stage = run.stage
        stage_key = stage.value
        requires_intervention = bool(result.get("requires_intervention", False))

        def apply(after: WorkflowState) -> None:
            nonlocal payload
            before_script = None
            if stage == WorkflowStage.SCRIPT_GENERATION:
                payload = self._drop_injected_params(after.artifacts.get(stage_key), payload)
                # 应用修订后的下游同步与手动 PATCH 走同一收尾：快照 MUST 在产物被覆写前取
                existing_script = after.artifacts.get(stage_key)
                before_script = (
                    script_revision.structure_snapshot(existing_script)
                    if isinstance(existing_script, dict)
                    else None
                )
                if requires_intervention:
                    # 【续写/修订/质检预览状态】预览只占用预览层，不替换正文（R08）
                    preview_key = None
                    if isinstance(payload, dict):
                        for key in ("pending_revision", "pending_review"):
                            if isinstance(payload.get(key), dict):
                                preview_key = key
                                break
                    if preview_key:
                        preview = payload[preview_key]
                        current_art = after.artifacts.get(stage_key)
                        current_art = copy.deepcopy(current_art) if isinstance(current_art, dict) else {}
                        if preview.get("base_version") != script_revision.content_fingerprint(current_art):
                            label = "修订" if preview_key == "pending_revision" else "质检"
                            raise ValueError(
                                f"剧本正文已变化，本次{label}预览已过期，请重新发起{label}"
                            )
                        current_art[preview_key] = copy.deepcopy(preview)
                        # 修订与质检预览互斥：同一时刻只允许一份待处理预览
                        for other_key in ("pending_revision", "pending_review"):
                            if other_key != preview_key:
                                current_art.pop(other_key, None)
                        after.artifacts[stage_key] = current_art
                    else:
                        # 续写预览保留 new_episodes 等增量字段供前端展示，不跨阶段同步
                        after.artifacts[stage_key] = copy.deepcopy(payload)
                else:
                    # 【确定续写 / 正常生成】先同步增量到第二、三阶段，再清理临时增量字段
                    self._sync_artifacts_cross_stages(after, stage, payload)
                    clean_art = copy.deepcopy(payload)
                    for key in workflow_store.PREVIEW_KEYS:
                        clean_art.pop(key, None)
                    after.artifacts[stage_key] = clean_art
                    source_episode_count = result.get("source_episode_count")
                    if source_episode_count is not None:
                        try:
                            after.meta["episodes"] = int(source_episode_count)
                        except (TypeError, ValueError):
                            logger.warning("[execute_stage] invalid source_episode_count=%r", source_episode_count)
            else:
                if run.background:
                    keys = list(workflow_store.ITEM_CONTAINERS.get(stage_key, ()))
                    targets = self._background_regeneration_targets(stage, run.intervention)
                    payload = self._merge_item_regeneration_payload(
                        after.artifacts.get(stage_key, {}), payload, keys, targets,
                        baseline_fields=self._stage_baseline(run),
                        protected_fields=_BACKGROUND_PROTECTED_FIELDS.get(stage_key, ()),
                    )
                else:
                    payload = self._prefer_user_edits(
                        stage_key, run.start_artifacts.get(stage_key), after.artifacts.get(stage_key), payload,
                    )
                self._sync_artifacts_cross_stages(after, stage, payload)
                # 阶段完成时保留失败条目的 error_type/error（payload 未携带时从上一版产物覆盖）
                self._preserve_item_error_fields(after.artifacts.get(stage_key), payload, stage)
                after.artifacts[stage_key] = payload

            # 重新计算所有阶段的状态（基于 artifacts 里的数据完整性）
            if not run.background and after.status.get(stage_key) == "running":
                after.status[stage_key] = "pending"
            # 重算前的状态基线：下游阶段里 `stopped`/`waiting` 不得因本次重跑被误标
            status_baseline = dict(after.status)
            self._recalculate_all_statuses(after)
            if result.get("stage_completed") and not requires_intervention:
                if after.status.get(stage_key) != "waiting":
                    after.status[stage_key] = "completed"
            elif requires_intervention:
                after.status[stage_key] = "waiting"
            # 剧本结构变更（应用修订）：同样在重算与收尾赋值之后才标记，否则被冲回 completed；
            # baseline MUST 与手动路径一样传重算前的快照，否则两条路径对 `stopped`/`waiting`
            # 的处理会不一致（修订路径会误标）
            if stage == WorkflowStage.SCRIPT_GENERATION and result.get("revision_applied"):
                self._sync_script_structure(after, before_script, baseline=status_baseline)
            # 分镜显式重跑：清理该集已成孤儿的下游条目引用，并把参考图/改写/视频标为待重新确认。
            # “旧 segments”以执行开始时的快照为准：运行中的 assets_preview 会先把已提交
            # 产物刷成新集，拿收尾时的实时 artifact 判孤儿会落空。
            if stage == WorkflowStage.STORYBOARD:
                rerun = self._rerun_episode_numbers(run.intervention)
                if rerun:
                    previous_storyboard = run.start_artifacts.get(stage_key)
                    self._prune_downstream_items_for_episodes(
                        after, rerun,
                        previous_storyboard if isinstance(previous_storyboard, dict) else {},
                        payload,
                        baseline=status_baseline,
                    )
            waiting = after.status.get(stage_key) == "waiting"
            after.stage_progress[stage_key] = {
                "phase": stage_key,
                "step": "等待确认" if waiting else "已完成",
                "message": "等待确认" if waiting else "已完成",
                "percent": 100,
                "updated_at": time.time(),
            }
            if not run.background:
                # 阶段进入终态：清除归属凭据（不再持有执行权）
                after.boot_id = None
                after.error = None

        def execution_states(after: WorkflowState) -> Dict[str, Dict[str, Any]]:
            if run.background:
                return {}
            execution = after.executions.get(stage_key) or {}
            final_leaves = {str(value) for value in workflow_store.iter_media(stage_key, after.artifacts.get(stage_key))}
            merged = [key for key in (execution.get("version_candidates") or {}) if key in final_leaves]
            return {stage_key: {"action": "finish", "execution_id": run.execution_id,
                                "status": "completed", "merged_candidates": merged}}

        with run.lock:
            state, _ = self._mutate(
                run.session_id, apply, published=published,
                execution_states=execution_states, publisher=run.publisher, publish_stage=stage_key,
            )
        run.finished = True
        return state

    def _fail_stage(self, run: "_StageRun", error: Optional[str],
                    missing_inputs: Optional[List[Dict[str, Any]]] = None) -> None:
        """失败/取消收尾：只改执行状态与错误，不动最后成功快照与用户已保存的工作稿。"""
        if run.finished:
            return
        stage_key = run.stage.value
        stopped = error is None or self.get_stop_event(run.session_id).is_set()
        message = "已取消" if stopped else "执行失败"

        def apply(after: WorkflowState) -> None:
            previous = (after.stage_progress.get(stage_key) or {}).get("percent", 0)
            if not run.background:
                after.status[stage_key] = "stopped" if stopped else "error"
                after.boot_id = None
            after.error = None if stopped else error
            after.stage_progress[stage_key] = {
                "phase": stage_key,
                "step": message,
                "message": message,
                "percent": previous,
                "updated_at": time.time(),
            }
            if missing_inputs and not stopped:
                after.stage_progress[stage_key]["missing_inputs"] = copy.deepcopy(missing_inputs)

        states = {} if run.background or not run.active_registered else {stage_key: {
            "action": "finish", "execution_id": run.execution_id,
            "status": "interrupted" if stopped else "failed", "error": error,
        }}
        try:
            with run.lock:
                self._mutate(run.session_id, apply, execution_states=states)
        except CommitStale:
            pass
        except Exception:
            logger.exception("stage failure commit failed: %s/%s", run.session_id, stage_key)
            self._evict(run.session_id)
        with self._state_lock:
            self._errors[run.session_id] = None if stopped else error
        run.finished = True

    @staticmethod
    def _drop_injected_params(previous: Any, payload: Any) -> Any:
        """剧本干预类输出由输入副本构造，去掉编排器注入的会话参数，不让它们进入产物。"""
        if not isinstance(payload, dict):
            return payload
        from core.agents.base_agent import SESSION_PARAM_KEYS

        existing = set(previous.keys()) if isinstance(previous, dict) else set()
        injected = (set(SESSION_PARAM_KEYS) | set(SESSION_META_KEYS) | {"script_source", "file_path"}) - existing
        # `episodes` 既是会话参数（集数，int），也是剧本产物的分集正文（list）：
        # 产物形态必须保留，否则首次生成的剧本会丢正文（只剩角色/场景）。
        if isinstance(payload.get("episodes"), list):
            injected.discard("episodes")
        return {key: value for key, value in payload.items() if key not in injected}

    @staticmethod
    def _prefer_user_edits(stage: str, base: Any, latest: Any, payload: Any) -> Any:
        """执行期间用户对同条目的修改胜出：晚到的模型结果只补充版本，不覆盖用户字段。

        注意：这是**全量路径**的保护，字段集合为 `_USER_EDIT_FIELDS`（含条目完成状态 `status`）。
        后台单条重生成不走这里，只保护 `_BACKGROUND_PROTECTED_FIELDS[stage]`
        （当前仅 `character_design.description`）；两者的差异是有意保留的，改动前
        先确认下游语义（name 是匹配键、species 决定路由）。
        """
        if not isinstance(payload, dict) or not isinstance(latest, dict) or base == latest:
            return payload
        merged = copy.deepcopy(payload)
        base = base if isinstance(base, dict) else {}
        for container in workflow_store.ITEM_CONTAINERS.get(stage, ()):
            items = merged.get(container)
            if not isinstance(items, list):
                continue
            base_items = {i.get("id"): i for i in base.get(container) or [] if isinstance(i, dict) and i.get("id")}
            latest_items = {i.get("id"): i for i in latest.get(container) or [] if isinstance(i, dict) and i.get("id")}
            for item in items:
                if not isinstance(item, dict):
                    continue
                current = latest_items.get(item.get("id"))
                if current is None:
                    continue
                original = base_items.get(item.get("id")) or {}
                for field in _USER_EDIT_FIELDS:
                    if field not in current:
                        continue
                    if field not in original:
                        # 基线缺该字段：仅当当前值非 None 才算用户新增（与旧语义逐字等价，
                        # 避免把 `species: null` 这类键写进产物）
                        if current.get(field) is not None:
                            item[field] = copy.deepcopy(current[field])
                    elif WorkflowEngine._field_edited_by_user(current, original, field):
                        item[field] = copy.deepcopy(current[field])
                item["versions"] = WorkflowEngine._merge_asset_versions(current.get("versions"), item.get("versions"))
            present = {item.get("id") for item in items if isinstance(item, dict)}
            for item_id, current in latest_items.items():
                if item_id not in present and item_id not in base_items:
                    items.append(copy.deepcopy(current))
        return merged

    async def continue_workflow(self, session_id: str, stage: Optional[str] = None) -> Dict:
        return await asyncio.to_thread(self._continue_workflow, session_id, stage)

    def _continue_workflow(self, session_id: str, stage: Optional[str] = None) -> Dict:
        if self.get_state(session_id) is None:
            return {
                "status": "error",
                "openclaw": "会话不存在，请刷新后重试。",
                "message": "会话不存在",
                "current_status": "missing",
            }
        response: Dict[str, Any] = {}

        def apply(after: WorkflowState) -> Any:
            current_stage_str = after.current_stage.value if hasattr(after.current_stage, "value") else str(after.current_stage)
            # 可选 stage：从哪个阶段点确认就只确认哪个阶段，不被归位后的 current_stage 误伤；
            # 非法/不在启用序列时回退 current_stage（旧客户端兼容）
            target_str = current_stage_str
            if isinstance(stage, str) and stage:
                enabled = [s.value for s in get_stage_order()]
                if stage in enabled:
                    target_str = stage
                else:
                    logger.warning("[continue_workflow] 非法 stage=%r，回退 current_stage=%r", stage, current_stage_str)
            logger.info(f"[continue_workflow] session={session_id}, target_stage={target_str}, current_stage={current_stage_str}, status={after.status}")
            # 在继续之前，先重新扫描一次状态，确保最新的选择已被计入
            self._recalculate_all_statuses(after)
            current_status = after.status.get(target_str)
            # 如果当前状态是 running，说明阶段还在执行中，不能继续
            if current_status == "running":
                response.update({
                    "status": "waiting",
                    "openclaw": f"当前阶段（{target_str}）还在执行中，请等待完成后再调用 /continue。",
                    "message": f"当前阶段（{target_str}）还在执行中，请等待完成后再调用 /continue。",
                    "current_status": "running",
                })
                return _NO_COMMIT
            # waiting/completed/reconfirm：用户确认后直接进入下一阶段
            # reconfirm 的语义是“接受现有产物并继续”：不重跑、不清空产物
            if current_status in ("waiting", "completed", "session_completed", STATUS_RECONFIRM):
                # 参考图阶段：非 reference 模式且无可用选中图时，拒绝直接置为已完成；
                # 否则“继续”会绕过“切回首帧后必须重新生成分镜图”的要求（design D7）
                if target_str == WorkflowStage.REFERENCE_GENERATION.value:
                    mode = str((after.meta or {}).get("video_generation_mode") or "first_frame")
                    if mode != "reference" and not self._reference_has_selection(after):
                        response.update({
                            "status": "error",
                            "openclaw": "参考图阶段缺少可用的分镜图：请先重新生成分镜参考图后再继续。",
                            "message": "需要先生成分镜图",
                            "current_status": current_status,
                        })
                        return _NO_COMMIT
                after.status[target_str] = "completed"
                # 确认 = 接受该阶段现有结果：无论原状态是什么都清掉它自己的过期详情，
                # 其它阶段的标记不动
                progress = after.stage_progress.get(target_str)
                if isinstance(progress, dict) and any(
                    key in progress for key in ("stale", "stale_episodes", "stale_reason", "stale_at")
                ):
                    after.stage_progress[target_str] = {
                        key: value for key, value in progress.items()
                        if key not in ("stale", "stale_episodes", "stale_reason", "stale_at")
                    }
                try:
                    target_stage = WorkflowStage(target_str)
                except ValueError:
                    target_stage = after.current_stage
                if isinstance(stage, str) and stage and not any(v == "running" for v in after.status.values()):
                    after.current_stage = target_stage
                next_stage = self._get_next_stage(target_stage)
                if not next_stage:
                    response.update({"status": "completed", "session_id": session_id})
                else:
                    response.update({"status": "ready", "next_stage": next_stage.value, "session_id": session_id})
                return None
            # 其他状态（如 pending, stopped, error）不允许继续
            response.update({
                "status": "error",
                "openclaw": f"当前状态 {current_status} 不允许继续，请检查会话状态。",
                "message": "当前状态不允许继续",
                "current_status": current_status,
            })
            return _NO_COMMIT

        state, _ = self._mutate(session_id, apply)
        # 所有分支都回到会话当前的状态表（包括 running/error 的 `_NO_COMMIT`）：
        # 前端需要据此同步全阶段视图，否则拒绝继续时界面会停在旧状态
        response["status_map"] = copy.deepcopy(state.status)
        if response.get("status") == "completed":
            media_refs.cleanup_session_refs(session_id)
        return response

    # ──────────── Artifact 统一管理 ────────────

    def update_artifact(self, session_id: str, stage: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a user edit (plus cross-stage sync) and commit it in one transaction."""
        sync_info: Dict[str, Any] = {}

        def apply(after: WorkflowState) -> None:
            # 变更前快照 MUST 在就地合并之前取（_apply_artifact_update 直接改 state 里的产物）；
            # 尚无剧本产物时传 None，首次写入不算“相对旧输入已变”
            previous_art = (
                after.artifacts.get(stage)
                if stage == WorkflowStage.SCRIPT_GENERATION.value
                else None
            )
            before_script = (
                script_revision.structure_snapshot(previous_art)
                if isinstance(previous_art, dict)
                else None
            )
            # 阶段状态基线同样要在任何重算之前取
            status_baseline = dict(after.status)
            self._apply_artifact_update(after, stage, copy.deepcopy(body) if isinstance(body, dict) else {})
            self._recalculate_all_statuses(after)
            # 下游同步与标记 MUST 排在重算之后，否则标记会被“产物完整即 completed”冲回
            touched_cast = isinstance(body, dict) and bool({"characters", "settings"} & set(body))
            sync_info.update(self._sync_script_structure(
                after, before_script, force_cast_sync=touched_cast, baseline=status_baseline,
            ) or {})

        state, _ = self._mutate(session_id, apply, publish_stage=stage)
        view = self._view_dict(state)
        response: Dict[str, Any] = {
            "status": "ok",
            "status_map": copy.deepcopy(view["status"]),
            "artifact": copy.deepcopy(view["artifacts"].get(stage)),
        }
        if stage == WorkflowStage.SCRIPT_GENERATION.value:
            # 形状稳定：未发生结构变更时也返回空列表/False，前端无需区分键存在性
            response["stale_storyboards"] = list(sync_info.get("stale_storyboards") or [])
            response["stale_final_videos"] = bool(sync_info.get("stale_final_videos"))
        return response

    @staticmethod
    def _stamp_manual_prompt(state: Any, merged: Dict[str, Any], seg_id: str) -> None:
        """用户保存/清空人工正文时，立刻用当前 artifacts 重算指纹与 prompt_source。

        PATCH 体只含产物字段，因此输入以会话 meta 为准（执行期会把 meta 合进
        input_data，两侧等价）。缺模型配置等异常不阻断保存：正文仍保留，指纹与
        解析链保持原值（不伪造 fresh）。
        """
        from core.agents.video_agent import VideoDirectorAgent
        from core.h3_modes import (
            collect_segments,
            current_fingerprint,
            resolve_grounding_model,
            resolve_style_key,
        )
        from core.prompt_source import resolve_effective_prompt

        artifacts = getattr(state, "artifacts", None)
        meta = getattr(state, "meta", None)
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        meta = meta if isinstance(meta, dict) else {}
        try:
            mode_key, video_model = VideoDirectorAgent._select_video_model({}, meta)
            h3_segments = collect_segments(
                artifacts.get("storyboard", {}), model=video_model,
                session_override=meta.get("video_duration"),
            )
            scene_map = {
                item.get("id"): item
                for item in (artifacts.get("reference_generation", {}) or {}).get("scenes", [])
                if isinstance(item, dict) and item.get("id")
            }
            _mode, fingerprint, dimensions = current_fingerprint(
                seg_id, mode_key, h3_segments, scene_map, artifacts,
                style=resolve_style_key({}, meta),
                grounding_model=resolve_grounding_model({}, meta),
            )
        except Exception as exc:  # noqa: BLE001 - 保存不被指纹计算阻断
            logger.warning("manual_prompt 指纹计算失败（保存仍生效）：%s", exc)
            # 人工正文存在时 source 是确定的（人工优先），只是新鲜度未知；
            # 清空操作失败则移除解析链，让前端落到“来源未知”。
            if str(merged.get("manual_prompt") or "").strip():
                merged["prompt_source"] = {
                    "source": "manual",
                    "freshness": "unknown",
                    "stale_reasons": ["fingerprint_missing"],
                }
            else:
                merged.pop("prompt_source", None)
            return

        if str(merged.get("manual_prompt") or "").strip():
            merged["manual_prompt_fingerprint"] = fingerprint
            merged["manual_prompt_dimensions"] = dimensions
        else:
            # 清空人工正文：同时清掉孤儿指纹/维度，避免 UI 误显“已保存”
            merged.pop("manual_prompt_fingerprint", None)
            merged.pop("manual_prompt_dimensions", None)

        # 刷新解析链：保存后前端立即读到 source=manual；清空后回到 H3/description
        h3_segment = next(
            (item for item in h3_segments if item.get("id") == seg_id),
            {"id": seg_id, "shot_content": "", "duration": 0, "characters": [], "location": ""},
        )
        items = (artifacts.get("prompt_rewrite", {}) or {}).get("items", [])
        h3_item = next(
            (item for item in items or [] if isinstance(item, dict) and item.get("id") == seg_id),
            None,
        )
        resolution = resolve_effective_prompt(
            h3_segment, h3_item, merged,
            current_fingerprint=fingerprint, current_dimensions=dimensions,
        )
        merged["prompt_source"] = {
            "source": resolution["source"],
            "freshness": resolution["freshness"],
            "stale_reasons": list(resolution["stale_reasons"]),
        }

    def _apply_artifact_update(self, state: WorkflowState, stage: str, body: Dict[str, Any]):
        """Apply a user edit to in-memory artifacts before the session is persisted."""
        # 派生字段（行区间/标注/场景映射）只能在最终合并之后由服务端重建，
        # 不能信任请求体里的旧值（R15）。
        rebuild_script_ranges = False
        rebuild_script_annotations = False
        if stage == "script_generation":
            # 取消请求显式携带 pending_revision / pending_review；正文级编辑使预览失效；
            # 仅改 title/logline 等元信息时保留预览（与内容指纹口径一致）。
            current_art = state.artifacts.get(stage)
            if isinstance(current_art, dict) and (
                "pending_revision" in body
                or "pending_review" in body
                or {"episodes", "characters", "settings"} & set(body)
            ):
                current_art.pop("pending_revision", None)
                current_art.pop("pending_review", None)
                # 正文/角色/场景被手工编辑后，质检撤销快照不再对应当前正文
                current_art.pop("review_rollback", None)
            if isinstance(current_art, dict) and "episodes" in body:
                # 客户端回写的派生字段一律丢弃，避免旧标注/旧 original_text 覆盖重建结果
                body.pop("line_annotations", None)
                body.pop("scene_spans", None)
                incoming_episodes = body.get("episodes") if isinstance(body.get("episodes"), list) else []
                if _has_import_coordinates(current_art):
                    rebuild_script_ranges = True
                    rebuild_script_annotations = (
                        _episode_body_signature(current_art.get("episodes"))
                        != _episode_body_signature(incoming_episodes)
                    )
                else:
                    for episode in incoming_episodes:
                        if isinstance(episode, dict):
                            episode.pop("line_start", None)
                            episode.pop("line_end", None)
            # 请求体中的 pending_revision / pending_review 只作为清除信号，不写入产物（避免 null 空键）
            body.pop("pending_revision", None)
            body.pop("pending_review", None)
        merge_keys_by_stage = {
            "character_design": ("characters", "settings"),
            "reference_generation": ("scenes",),
            "prompt_rewrite": ("items",),
            "video_generation": ("clips",),
        }
        if stage in merge_keys_by_stage:
            current_art = state.artifacts.get(stage, {})
            if isinstance(current_art, dict):
                for list_key in merge_keys_by_stage[stage]:
                    if list_key not in body:
                        continue
                    current_items = current_art.get(list_key, [])
                    incoming_items = body.get(list_key, [])
                    if not isinstance(current_items, list) or not isinstance(incoming_items, list):
                        continue
                    current_by_id = {
                        item.get("id"): item
                        for item in current_items
                        if isinstance(item, dict) and item.get("id")
                    }
                    merged_items = []
                    seen_ids = set()
                    for incoming in incoming_items:
                        if not isinstance(incoming, dict) or not incoming.get("id"):
                            continue
                        item_id = incoming["id"]
                        current = current_by_id.get(item_id, {})
                        merged = {**current, **incoming}
                        explicit_video_selection = (
                            stage == "video_generation"
                            and "selected" in incoming
                        )
                        if current.get("selected") and not incoming.get("selected") and not explicit_video_selection:
                            merged["selected"] = current.get("selected")
                        merged["versions"] = self._merge_asset_versions(
                            current.get("versions"),
                            incoming.get("versions"),
                        )
                        if stage == "video_generation" and "manual_prompt" in incoming:
                            self._stamp_manual_prompt(state, merged, item_id)
                        if current.get("selected") and incoming.get("status") in {"failed", "pending"}:
                            merged["status"] = current.get("status", "done")
                        merged_items.append(merged)
                        seen_ids.add(item_id)
                    for current in current_items:
                        if isinstance(current, dict) and current.get("id") not in seen_ids:
                            merged_items.append(current)

                    # 提示词改写：用户直接编辑改写文本时记录版本（旧文本 superseded、新文本 user）
                    if stage == "prompt_rewrite":
                        for merged in merged_items:
                            prev = current_by_id.get(merged.get("id")) or {}
                            new_text = str(merged.get("rewritten_prompt") or "")
                            old_text = str(prev.get("rewritten_prompt") or "")
                            if not new_text or new_text == old_text:
                                continue
                            versions = merged.get("versions") if isinstance(merged.get("versions"), list) else []
                            if old_text and not any(
                                isinstance(v, dict) and v.get("content") == old_text for v in versions
                            ):
                                versions.append({
                                    "content": old_text, "source": "superseded",
                                    "created_at": datetime.now().isoformat(),
                                })
                            if not any(
                                isinstance(v, dict) and v.get("content") == new_text and v.get("source") == "user"
                                for v in versions
                            ):
                                versions.append({
                                    "content": new_text, "source": "user",
                                    "created_at": datetime.now().isoformat(),
                                })
                            merged["versions"] = versions
                    body[list_key] = merged_items

        if stage == "storyboard" and any(k in body for k in ("episodes", "segments", "shots")):
            for shot in body.get('shots', []):
                if isinstance(shot, dict) and 'is_new' in shot:
                    shot['is_new'] = False

            input_segments = list(body.get('segments', []))
            for ep in body.get('episodes', []):
                if isinstance(ep, dict):
                    input_segments.extend(seg for seg in ep.get('segments', []) if isinstance(seg, dict))

            seg_info_list = []
            for seg in input_segments:
                seg_id = seg.get('segment_id')
                if not seg_id:
                    continue
                shots = seg.get('shots', [])
                desc_video = " ".join([sh.get("plot") or sh.get("content") or "" for sh in shots]).strip()
                total_dur = seg.get("total_duration") or sum([sh.get("duration", 0) for sh in shots]) or 10
                seg_info_list.append({
                    "segment_id": seg_id,
                    "desc": desc_video,
                    "duration": total_dur,
                    "visual_prompt": seg.get("visual_prompt", ""),
                })

            video_art = state.artifacts.get('video_generation', {})
            if isinstance(video_art, dict) and isinstance(video_art.get('clips'), list):
                for clip in video_art['clips']:
                    target = next((item for item in seg_info_list if item["segment_id"] == clip.get('id')), None)
                    if target:
                        clip['duration'] = target['duration']
                        clip['description'] = target['desc']

            ref_art = state.artifacts.get('reference_generation', {})
            if isinstance(ref_art, dict) and isinstance(ref_art.get('scenes'), list):
                for scene in ref_art['scenes']:
                    target = next((item for item in seg_info_list if item["segment_id"] == scene.get('id')), None)
                    if target and target.get("visual_prompt"):
                        scene['description'] = target['visual_prompt']

            if "segments" in body and "episodes" not in body:
                body = {k: v for k, v in body.items() if k != "segments"}
            body.pop('new_shot_ids', None)

        elif stage == "reference_generation":
            if "segments" in body:
                seg_id_to_prompt = {
                    s['segment_id']: s.get('visual_prompt', '')
                    for s in body['segments']
                    if isinstance(s, dict) and 'segment_id' in s
                }

                storyboard_art = state.artifacts.get('storyboard', {})
                if isinstance(storyboard_art, dict):
                    for ep in storyboard_art.get('episodes', []):
                        if not isinstance(ep, dict):
                            continue
                        for seg in ep.get('segments', []):
                            if isinstance(seg, dict) and seg.get('segment_id') in seg_id_to_prompt:
                                seg['visual_prompt'] = seg_id_to_prompt[seg.get('segment_id')]

                ref_art = state.artifacts.get('reference_generation', {})
                if isinstance(ref_art, dict):
                    for scene in ref_art.get('scenes', []):
                        if isinstance(scene, dict) and scene.get('id') in seg_id_to_prompt:
                            scene['description'] = seg_id_to_prompt[scene.get('id')]

                body = {k: v for k, v in body.items() if k != "segments"}

            ref_art = state.artifacts.get('reference_generation', {})
            if isinstance(ref_art, dict):
                scenes = ref_art.get('scenes', [])
                is_selection_format = any(isinstance(k, str) and not isinstance(v, (list, dict)) for k, v in body.items())
                if is_selection_format and scenes:
                    for scene in scenes:
                        if isinstance(scene, dict) and scene.get('id') in body:
                            scene['selected'] = body[scene.get('id')]
                    body = {}

        elif stage == "video_generation":
            clip_id_to_duration = {}
            clip_id_to_description = {}
            for clip_id, value in body.items():
                if isinstance(value, dict):
                    if 'duration' in value:
                        clip_id_to_duration[clip_id] = value['duration']
                    if 'description' in value:
                        clip_id_to_description[clip_id] = value['description']

            if clip_id_to_duration or clip_id_to_description:
                storyboard_art = state.artifacts.get('storyboard', {})
                if isinstance(storyboard_art, dict):
                    for ep in storyboard_art.get('episodes', []):
                        if not isinstance(ep, dict):
                            continue
                        for seg in ep.get('segments', []):
                            if isinstance(seg, dict) and seg.get('segment_id') in clip_id_to_duration:
                                seg['total_duration'] = clip_id_to_duration[seg.get('segment_id')]

                vid_art = state.artifacts.get('video_generation', {})
                if isinstance(vid_art, dict):
                    for clip in vid_art.get('clips', []):
                        if not isinstance(clip, dict):
                            continue
                        clip_id = clip.get('id')
                        if clip_id in clip_id_to_duration:
                            clip['duration'] = clip_id_to_duration[clip_id]
                        if clip_id in clip_id_to_description:
                            clip['description'] = clip_id_to_description[clip_id]

            # 键控形状仅兼容 duration/description；命中片段 ID 的其余字段（含
            # manual_prompt）不得写进 artifact 顶层——消费后丢弃并记日志；
            # 非片段 ID 的字典键（如 selected_images）保持旧行为。
            consumed_ids = set(clip_id_to_duration) | set(clip_id_to_description)
            existing_clip_ids = {
                item.get("id")
                for item in (state.artifacts.get("video_generation", {}) or {}).get("clips", [])
                if isinstance(item, dict) and item.get("id")
            }
            for key, value in list(body.items()):
                if key in ("clips", "video_clips") or not isinstance(value, dict):
                    continue
                if key in consumed_ids:
                    body.pop(key, None)
                    continue
                if key in existing_clip_ids or "manual_prompt" in value:
                    logger.warning(
                        "video_generation 键控 PATCH 丢弃未支持字段：%s=%s（请改用 clips 数组）",
                        key, sorted(value),
                    )
                    body.pop(key, None)

            vid_art = state.artifacts.get('video_generation', {})
            if isinstance(vid_art, dict):
                clips = vid_art.get('clips', [])
                is_selection_format = any(isinstance(k, str) and not isinstance(v, (list, dict)) for k, v in body.items())
                if is_selection_format and clips:
                    for clip in clips:
                        if isinstance(clip, dict) and clip.get('id') in body:
                            clip['selected'] = body[clip.get('id')]
                    body = {}

        current = state.artifacts.get(stage)
        if current is None:
            state.artifacts[stage] = body
        elif isinstance(current, dict):
            current.update(body)
        else:
            state.artifacts[stage] = body

        if rebuild_script_ranges:
            final_art = state.artifacts.get(stage)
            if isinstance(final_art, dict):
                lines = _assign_episode_line_ranges(final_art.get("episodes") or [])
                if rebuild_script_annotations:
                    _rebuild_import_annotations(final_art, lines)

    def upload_artifact_image(
        self,
        session_id: str,
        stage: str,
        item_type: str,
        item_id: str,
        file_obj: Any,
        filename: str = "",
    ) -> Dict[str, Any]:
        """Save a user-provided image and attach it to the target artifact item."""
        # 必须在 get_state（读盘）/创建目录/写文件之前校验
        validate_upload_identifier(str(session_id), "session_id")
        validate_upload_identifier(str(item_id), "item_id")
        allowed_exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
        ext = os.path.splitext(filename or "")[1].lower() or ".png"
        if ext not in allowed_exts:
            raise ValueError(f"仅支持 {', '.join(sorted(allowed_exts))} 格式的图片")

        state = self.get_state(session_id)
        if not state:
            raise KeyError(f"Session not found: {session_id}")

        # 跳过形态的接口防线：目标条目不存在时不得凭空新建（design D9）
        if stage == WorkflowStage.REFERENCE_GENERATION.value and item_type == "scenes":
            existing = state.artifacts.get(stage)
            if reference_skip_marker(existing):
                known_ids = {
                    item.get("id")
                    for item in existing.get("scenes") or []
                    if isinstance(item, dict) and item.get("id")
                }
                if item_id not in known_ids:
                    raise ValueError(
                        f"目标分镜条目不存在：{item_id}（参考图阶段处于跳过形态，不会新建条目）"
                    )

        cfg = self._upload_item_config(stage, item_type, item_id)
        validate_upload_identifier(str(cfg.get("base") or ""), "item_id", UPLOAD_BASE_PATTERN)
        # 人工媒体上传也是一次独立 execution：先写受信工作根，再发布为不可变对象并同事务提交
        publisher = workflow_store.ExecutionPublisher(
            owner=state.owner, session_id=session_id, stage=stage,
            execution_id=new_execution_id(), boot_id=self._boot_id,
        )
        save_dir = os.path.join(publisher.work_root, "uploads")
        os.makedirs(save_dir, exist_ok=True)
        local_path = os.path.join(save_dir, f"{cfg['base']}_upload{ext}")
        try:
            with open(local_path, "wb") as buffer:
                shutil.copyfileobj(file_obj, buffer)
        except Exception as exc:
            raise RuntimeError(f"图片保存失败: {exc}") from exc

        def apply(after: WorkflowState) -> None:
            artifact = after.artifacts.get(stage)
            artifact = artifact if isinstance(artifact, dict) else {}
            after.artifacts[stage] = artifact
            items = artifact.setdefault(cfg["list_key"], [])
            if not isinstance(items, list):
                items = []
                artifact[cfg["list_key"]] = items
            target = next((item for item in items if isinstance(item, dict) and item.get("id") == item_id), None)
            if target is None:
                target = {"id": item_id, "name": item_id, "description": "", "versions": []}
                items.append(target)
            versions = target.get("versions")
            if not isinstance(versions, list):
                versions = []
            if local_path not in versions:
                versions.append(local_path)
            target["versions"] = versions
            target["selected"] = local_path
            target["status"] = "done"
            self._recalculate_all_statuses(after)

        state, _ = self._mutate(session_id, apply, publisher=publisher, publish_stage=stage)
        view = self._view_dict(state)
        artifact = view["artifacts"].get(stage) or {}
        target = next(
            (item for item in artifact.get(cfg["list_key"]) or [] if isinstance(item, dict) and item.get("id") == item_id),
            {},
        )
        return {
            "status": "ok",
            "path": target.get("selected", ""),
            "item_id": item_id,
            "item_type": item_type,
            "artifact": copy.deepcopy(artifact),
            "status_map": copy.deepcopy(view["status"]),
        }

    @staticmethod
    def _upload_item_config(stage: str, item_type: str, item_id: str) -> Dict[str, str]:
        if stage == "character_design" and item_type == "characters":
            base = item_id if item_id.startswith("char_") else f"char_{item_id}"
            return {"list_key": "characters", "dir": os.path.join("Assets", "characters"), "base": base}
        if stage == "character_design" and item_type == "settings":
            base = item_id if item_id.startswith("set_") else f"set_{item_id}"
            return {"list_key": "settings", "dir": os.path.join("Assets", "settings"), "base": base}
        if stage == "reference_generation" and item_type == "scenes":
            return {"list_key": "scenes", "dir": "Scenes", "base": item_id}
        raise ValueError("Unsupported upload target")

    # ──────────── 会话持久化 ────────────

    def load_sessions(self) -> int:
        """lifespan 入口：只恢复上一进程遗留的 running；会话按需从 DB 懒加载（不预读全量、不读 JSON）。"""
        for sid in sessions_with_running():
            if _is_valid_upload_identifier(sid):
                self._load(sid)
        return len(session_ids())

    def delete_session(self, session_id: str) -> bool:
        """删除会话：同一事务先登记全部对象/精确本地文件的清理定位，再删父行级联（D17）。

        物理删除由清理器异步完成（delete+stat 复核、失败退避重试）；历史 JSON 不改不删。
        """
        from core.object_operations import register_resource_delete

        if not _is_valid_upload_identifier(session_id):
            logger.warning("Invalid session identifier rejected before delete: %r", session_id)
            return False

        with self._session_lock(session_id):
            with self._state_lock:
                if session_id in self._active_sessions:
                    self.get_stop_event(session_id).set()
                    logger.warning(f"Refusing to delete active session: {session_id}")
                    return False
            state = self._load(session_id, recover=False)
            if state is None:
                return False
            owner = state.owner
            deleted = delete_session_cascade(
                session_id,
                owner,
                lambda cur: register_resource_delete(cur, owner=owner, domain="sessions", resource_id=session_id),
            )
            self._evict(session_id)
            with self._state_lock:
                self._stop_events.pop(session_id, None)
                self._overlays.pop(session_id, None)
                self._errors.pop(session_id, None)
        if deleted:
            grounding_cache.clear_session(session_id)
        logger.info("Session deleted (object cleanup queued): %s", session_id)
        return deleted

    def cleanup_orphan_results(self, dry_run: bool = False) -> Dict[str, Any]:
        """Remove legacy result dirs whose session no longer exists in the DB; dry_run only reports."""
        from config import settings

        known = session_ids()
        cleaned = {"scripts": [], "images": [], "videos": []}
        result_base = settings.RESULT_DIR

        script_dir = os.path.join(result_base, 'script')
        if os.path.isdir(script_dir):
            for filename in os.listdir(script_dir):
                if not filename.endswith('.json'):
                    continue
                sid = filename[:-5]
                if sid not in known:
                    if not dry_run:
                        os.remove(os.path.join(script_dir, filename))
                    cleaned["scripts"].append(sid)

        image_dir = os.path.join(result_base, 'image')
        if os.path.isdir(image_dir):
            for dirname in os.listdir(image_dir):
                if dirname != 'test_avail' and dirname not in known:
                    if not dry_run:
                        shutil.rmtree(os.path.join(image_dir, dirname))
                    cleaned["images"].append(dirname)

        video_dir = os.path.join(result_base, 'video')
        if os.path.isdir(video_dir):
            for dirname in os.listdir(video_dir):
                if dirname != 'test_avail' and dirname not in known:
                    if not dry_run:
                        shutil.rmtree(os.path.join(video_dir, dirname))
                    cleaned["videos"].append(dirname)

        return {"status": "cleaned", "cleaned": cleaned}

    def get_scene_asset_counts(self, session_id: str, scene_number: int) -> Dict[str, Any]:
        """Count generated reference images/videos that are indexed and not marked missing."""
        state = self.get_state(session_id)
        if not state:
            raise KeyError(f"Session not found: {session_id}")
        artifacts = copy.deepcopy(state.artifacts)
        owner = state.owner

        storyboard = artifacts.get('storyboard', {})
        segment_ids = self._segment_ids_for_scene(storyboard, scene_number)

        ref_artifact = artifacts.get('reference_generation', {})
        ref_scenes = ref_artifact.get('scenes', []) if isinstance(ref_artifact, dict) else []
        ref_image_count = self._count_existing_assets(ref_scenes, segment_ids, owner, include_versions=True)

        video_artifact = artifacts.get('video_generation', {})
        video_clips = video_artifact.get('clips', []) if isinstance(video_artifact, dict) else []
        video_count = self._count_existing_assets(video_clips, segment_ids, owner, include_versions=False)

        return {
            "scene_number": scene_number,
            "reference_images": ref_image_count,
            "videos": video_count,
            "shot_count": len(segment_ids),
        }

    @staticmethod
    def _segment_ids_for_scene(storyboard: Any, scene_number: int) -> List[str]:
        if not isinstance(storyboard, dict):
            return []

        ids: List[str] = []
        for shot in storyboard.get('shots', []):
            if isinstance(shot, dict) and shot.get('scene_number') == scene_number and shot.get('shot_id'):
                ids.append(shot['shot_id'])

        for episode in storyboard.get('episodes', []):
            if not isinstance(episode, dict):
                continue
            for segment in episode.get('segments', []):
                if not isinstance(segment, dict):
                    continue
                segment_scene = segment.get('scene_number') or segment.get('segment_number')
                if segment_scene == scene_number and segment.get('segment_id'):
                    ids.append(segment['segment_id'])

        return list(dict.fromkeys(ids))

    @staticmethod
    def _asset_exists(owner: str, path: str) -> bool:
        """资产可用 = 已索引、归属本人且未标缺失（不靠本地 os.path 判定 MinIO 对象）。"""
        if not path:
            return False
        from db.repositories import FileRepo

        public = media_refs.to_public(str(path))
        if not public.startswith(media_refs.PUBLIC_PREFIX):
            return False
        row = FileRepo().get_by_public_path(public)
        return bool(row) and str(row.get("owner") or "") == owner and row.get("missing_at") is None

    @classmethod
    def _count_existing_assets(
        cls,
        items: List[Any],
        target_ids: List[str],
        owner: str,
        *,
        include_versions: bool,
    ) -> int:
        count = 0
        target_set = set(target_ids)
        for item in items:
            if not isinstance(item, dict) or item.get('id') not in target_set:
                continue
            selected = item.get('selected')
            if selected and cls._asset_exists(owner, selected):
                count += 1
            if include_versions:
                versions = item.get('versions', [])
                for version in versions if isinstance(versions, list) else []:
                    if version and version != selected and cls._asset_exists(owner, version):
                        count += 1
        return count

    def list_saved_sessions(self, owner: Optional[str] = None) -> List[Dict]:
        """列出 owner 的会话概要（DB 读模型；本进程运行中的进度覆盖层一并叠加）。"""
        if not owner:
            raise ValueError("owner is required")
        with self._state_lock:
            overlays = copy.deepcopy(self._overlays)
        sessions: List[Dict] = []
        for row in list_session_summaries(owner):
            sid = str(row.get("session_id") or "")
            meta = workflow_store.session_meta(row.get("meta_json"))
            status = {
                key: ("stopped" if value == "interrupted" else value)
                for key, value in (row.get("status_json") or {}).items()
            }
            stage_progress = copy.deepcopy(row.get("stage_progress_json") or {})
            for stage, overlay in (overlays.get(sid) or {}).items():
                if overlay.get("progress"):
                    stage_progress[stage] = overlay["progress"]
            title = row.get("title") or meta.get("idea") or meta.get("user_textbox_input") or ""
            sessions.append({
                "id": sid,
                "title": title,
                "idea": meta.get("idea") or meta.get("user_textbox_input") or "",
                "style": meta.get("style") or "",
                "date": (int(row.get("updated_at") or 0) / 1000.0),
                "status": status,
                "current_stage": row.get("current_stage") or WorkflowStage.INIT.value,
                "meta": meta,
                "stage_progress": stage_progress,
            })
        sessions.sort(key=lambda x: x.get("date", 0), reverse=True)
        return sessions

    def owner_of(self, session_id: str) -> Optional[str]:
        """Return the session owner from the committed DB view (missing session → None)."""
        state = self.get_state(session_id)
        return state.owner if state else None
