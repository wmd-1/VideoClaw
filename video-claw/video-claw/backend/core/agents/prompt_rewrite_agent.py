# -*- coding: utf-8 -*-
"""
提示词改写阶段 Agent（prompt_rewrite）。

调用 Design Agent Platform 的 minimax-h3-prompt-writing 能力，把每个分镜的
镜头描述改写为符合 MiniMax H3 规范的视频提示词。逐分镜串行提交、条目级失败隔离；
产物结构与 reference_generation.scenes 对齐（id = storyboard segment_id）。

介入契约（intervention）：
- {"regenerate_items": [segment_id, ...]}：后台单条重生成（复用外部会话追加轮次）；
- {"revise_items": [{"id": segment_id, "instruction": "..."}, ...]}：按用户修改意见
  向同一外部会话追加一轮修订。
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import Config
from core.agents.base_agent import AgentInterface
from prompts.loader import load_prompt

logger = logging.getLogger(__name__)

# MiniMax H3 支持的视频时长范围（秒）；超出夹取到边界
H3_DURATION_MIN = 4
H3_DURATION_MAX = 15
H3_DURATION_DEFAULT = 8

_CODE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```", re.DOTALL)


def _clamp_duration(raw: Any) -> int:
    """夹取到 H3 支持的 4~15 秒；缺失/非法/非正值回退默认 8 秒。"""
    try:
        value = int(round(float(raw)))
    except (TypeError, ValueError):
        return H3_DURATION_DEFAULT
    if value <= 0:
        return H3_DURATION_DEFAULT
    return max(H3_DURATION_MIN, min(H3_DURATION_MAX, value))


def _extract_prompt(assistant_text: str) -> str:
    """从外部 agent 回复中提取最终提示词：优先取 markdown 代码块，否则整段使用。"""
    text = str(assistant_text or "").strip()
    if not text:
        return ""
    matches = _CODE_FENCE_RE.findall(text)
    if matches:
        return max(matches, key=len).strip()
    return text


class PromptRewriteAgent(AgentInterface):
    """提示词改写：分镜文本 → MiniMax H3 规范提示词（经 Design Agent Platform）"""

    def __init__(self):
        super().__init__(name="prompt_rewrite")

    # ─── 输入收集 ───

    @staticmethod
    def _collect_segments(storyboard_artifact: Dict) -> List[Dict[str, Any]]:
        """从 storyboard 产物收集分镜条目（id/描述/时长），保持 segment 顺序。"""
        segments: List[Dict[str, Any]] = []
        episodes = storyboard_artifact.get("episodes", [])
        if not isinstance(episodes, list):
            return segments
        for ep in episodes:
            if not isinstance(ep, dict):
                continue
            ep_n = ep.get("episode_number", 0)
            for s_i, seg in enumerate(ep.get("segments", []), 1):
                if not isinstance(seg, dict):
                    continue
                seg_id = seg.get("segment_id") or f"seg_{ep_n:02d}_{s_i:02d}"
                shots = seg.get("shots", [])
                shot_content = "\n".join(
                    str(sh.get("content") or sh.get("plot") or "").strip()
                    for sh in shots
                    if isinstance(sh, dict)
                ).strip()
                total_duration = seg.get("total_duration") or sum(
                    sh.get("duration", 0) for sh in shots if isinstance(sh, dict)
                )
                segments.append({
                    "id": seg_id,
                    "name": f"第{ep_n}集-片段{s_i}",
                    "index": s_i,
                    "episode": ep_n,
                    "shot_content": shot_content,
                    "duration": _clamp_duration(total_duration),
                })
        segments.sort(key=lambda x: x["id"])
        return segments

    def _build_request_text(self, segment: Dict[str, Any]) -> str:
        template = load_prompt("prompt_rewrite", "rewrite", "zh")
        return template.format(
            duration=segment["duration"],
            segment_id=segment["id"],
            shot_content=segment["shot_content"],
        )

    # ─── 外部会话管理 ───

    @staticmethod
    def _create_client():
        from models.design_agent_client import DesignAgentClient

        return DesignAgentClient()

    def _ensure_session(self, prev_session_id: Optional[str]):
        """复用既有外部会话；失效（404/410/409/不可恢复）时重建。返回 (client, session_id)。"""
        client = self._create_client()
        if prev_session_id:
            try:
                client.get_session(prev_session_id)
                return client, prev_session_id
            except Exception as exc:  # noqa: BLE001 - 任何复用失败都回退到重建
                logger.warning("prompt_rewrite: 复用外部会话 %s 失败（%s），重建", prev_session_id, exc)
                try:
                    self._report_progress("提示词改写", "外部会话失效，重建中...", 5)
                except Exception:  # noqa: BLE001
                    pass
        return client, client.create_session()

    # ─── 条目构造 ───

    @staticmethod
    def _make_item(segment: Dict[str, Any], rewritten_prompt: str, original_prompt: str,
                   prev_item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "id": segment["id"],
            "name": segment["name"],
            "index": segment["index"],
            "episode": segment.get("episode"),
            "original_prompt": original_prompt,
            "rewritten_prompt": rewritten_prompt,
            "input_mode": "T2VA",
            "duration": segment["duration"],
            "status": "done" if rewritten_prompt else "failed",
            "selected": (prev_item or {}).get("selected", ""),
            "versions": (prev_item or {}).get("versions", []),
        }
        if not rewritten_prompt:
            item["error"] = "改写结果为空"
        return item

    @staticmethod
    def _append_version(item: Dict[str, Any], content: str, source: str) -> None:
        versions = item.setdefault("versions", [])
        if not isinstance(versions, list):
            versions = []
            item["versions"] = versions
        versions.append({"content": content, "source": source, "created_at": datetime.now().isoformat()})

    # ─── 核心执行 ───

    async def _rewrite_segments(self, client, session_id: str, segments: List[Dict[str, Any]],
                                prev_items_by_id: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """逐分镜串行改写；条目级失败隔离，实时透出条目完成事件。"""
        results: Dict[str, Dict[str, Any]] = {}
        total = len(segments)
        for i, segment in enumerate(segments):
            self._check_cancel()
            item = self._make_item(segment, "", segment["shot_content"], prev_items_by_id.get(segment["id"]))
            try:
                # 同步阻塞调用放进线程池，避免卡死事件循环
                request_text = self._build_request_text(segment)
                assistant_text = await asyncio.to_thread(client.submit_turn, session_id, request_text)
                rewritten = _extract_prompt(assistant_text)
                item = self._make_item(segment, rewritten, segment["shot_content"], prev_items_by_id.get(segment["id"]))
                if rewritten:
                    self._append_version(item, rewritten, source="agent")
            except Exception as exc:  # noqa: BLE001 - 单条目失败不阻塞其他条目
                logger.warning("prompt_rewrite: 分镜 %s 改写失败: %s", segment["id"], exc)
                item = self._make_item(segment, "", segment["shot_content"], prev_items_by_id.get(segment["id"]))
                item["status"] = "failed"
                item["error"] = str(exc)
            results[segment["id"]] = item
            self._report_progress(
                "提示词改写",
                f"分镜 {segment['id']} {'完成' if item['status'] == 'done' else '失败'}",
                round((i + 1) / total * 100),
                data={
                    "asset_complete": {
                        "type": "items",
                        "id": segment["id"],
                        "status": item["status"],
                        "error": item.get("error"),
                    }
                },
            )
        return results

    async def process(self, input_data: Any, intervention: Optional[Dict] = None) -> Dict:
        # 前置校验：未启用/未配置服务地址时明确失败（不静默回退）
        self.check_enabled()
        input_data = self._merge_session_params(input_data if isinstance(input_data, dict) else {})
        artifacts = self._session_artifacts(input_data)
        storyboard_artifact = self._session_artifact(input_data, "storyboard")
        prev_artifact = artifacts.get("prompt_rewrite") if isinstance(artifacts.get("prompt_rewrite"), dict) else {}
        prev_session_id = prev_artifact.get("session_id")
        prev_items = prev_artifact.get("items", []) if isinstance(prev_artifact.get("items"), list) else []
        prev_items_by_id = {item.get("id"): item for item in prev_items if isinstance(item, dict) and item.get("id")}

        segments = self._collect_segments(storyboard_artifact)
        if not segments:
            raise ValueError("提示词改写阶段缺少分镜数据：请先完成分镜设计阶段")

        # ── 介入：按用户修改意见追加轮次修订 ──
        if isinstance(intervention, dict) and isinstance(intervention.get("revise_items"), list) and intervention["revise_items"]:
            return await self._process_revisions(
                intervention["revise_items"], segments, prev_items_by_id, prev_session_id
            )

        # ── 介入：后台单条重生成（只处理指定条目，由编排器合并） ──
        regenerate_items: List[str] = []
        if isinstance(intervention, dict) and isinstance(intervention.get("regenerate_items"), list):
            regenerate_items = [str(x) for x in intervention["regenerate_items"] if x]
        if regenerate_items:
            target_segments = [seg for seg in segments if seg["id"] in set(regenerate_items)]
            missing = [sid for sid in regenerate_items if sid not in {seg["id"] for seg in target_segments}]
            if missing:
                raise ValueError(f"待重生成的分镜不存在：{', '.join(missing)}")
            client, ext_session_id = await asyncio.to_thread(
                self._ensure_session, prev_session_id
            )
            try:
                results = await self._rewrite_segments(client, ext_session_id, target_segments, prev_items_by_id)
            finally:
                client.close()
            items = [results[sid] for sid in regenerate_items if sid in results]
            return {"payload": {"session_id": ext_session_id, "items": items}, "requires_intervention": True}

        # ── 首次 / 全量执行 ──
        self._report_progress("提示词改写", "开始改写分镜提示词...", 2)
        client, ext_session_id = await asyncio.to_thread(self._ensure_session, prev_session_id)
        try:
            results = await self._rewrite_segments(client, ext_session_id, segments, prev_items_by_id)
        finally:
            client.close()

        items = [results[seg["id"]] for seg in segments if seg["id"] in results]
        # 保留上一版产物中、本次未覆盖的条目（如 storyboard 删减后遗留的历史条目丢弃即可）
        done_count = sum(1 for item in items if item["status"] == "done")
        self._report_progress("提示词改写", f"改写完成（{done_count}/{len(items)} 成功）", 100)
        return {
            "payload": {"session_id": ext_session_id, "items": items},
            "requires_intervention": True,
            "stage_completed": True,
        }

    async def _process_revisions(self, revise_items: List[Dict], segments: List[Dict[str, Any]],
                                 prev_items_by_id: Dict[str, Dict[str, Any]], prev_session_id: Optional[str]) -> Dict:
        seg_by_id = {seg["id"]: seg for seg in segments}
        client, ext_session_id = await asyncio.to_thread(self._ensure_session, prev_session_id)
        results: Dict[str, Dict[str, Any]] = {}
        try:
            total = len(revise_items)
            for i, revision in enumerate(revise_items):
                self._check_cancel()
                if not isinstance(revision, dict):
                    continue
                seg_id = str(revision.get("id") or "")
                instruction = str(revision.get("instruction") or "").strip()
                seg = seg_by_id.get(seg_id)
                prev_item = prev_items_by_id.get(seg_id)
                if not seg or not prev_item or not instruction:
                    continue
                revise_text = (
                    f"请在上一版提示词基础上修订分镜 {seg_id} 的改写结果。\n"
                    f"修改意见：{instruction}\n"
                    f"目标视频时长保持 {prev_item.get('duration', seg['duration'])} 秒。"
                    f"只输出修订后的完整提示词。"
                )
                item = dict(prev_item)
                try:
                    assistant_text = await asyncio.to_thread(client.submit_turn, ext_session_id, revise_text)
                    rewritten = _extract_prompt(assistant_text)
                    if not rewritten:
                        raise ValueError("修订结果为空")
                    old_text = prev_item.get("rewritten_prompt") or ""
                    self._append_version(prev_item, old_text, source="superseded")
                    item = self._make_item(seg, rewritten, prev_item.get("original_prompt") or seg["shot_content"], prev_item)
                    item["rewritten_prompt"] = rewritten
                    self._append_version(item, rewritten, source="agent")
                except Exception as exc:  # noqa: BLE001 - 单条目失败隔离
                    logger.warning("prompt_rewrite: 分镜 %s 修订失败: %s", seg_id, exc)
                    item = dict(prev_item)
                    item["status"] = "failed"
                    item["error"] = str(exc)
                results[seg_id] = item
                self._report_progress(
                    "提示词改写",
                    f"分镜 {seg_id} 修订{'完成' if item.get('status') == 'done' else '失败'}",
                    round((i + 1) / total * 100),
                    data={"asset_complete": {"type": "items", "id": seg_id, "status": item.get("status"), "error": item.get("error")}},
                )
        finally:
            client.close()
        items = [results[sid] for sid in results]
        return {"payload": {"session_id": ext_session_id, "items": items}, "requires_intervention": True}

    @staticmethod
    def check_enabled() -> None:
        """阶段执行前置校验：未启用或未配置服务地址时抛出明确错误（由编排器/路由层调用）。"""
        if not Config.DESIGN_AGENT_ENABLED:
            raise ValueError(
                "提示词改写阶段未启用：请在设置页「提示词改写服务」开启 enable，"
                "或在 .env 配置 VC_DESIGN_AGENT__ENABLE=true"
            )
        if not str(Config.DESIGN_AGENT_BASE_URL or "").strip():
            raise ValueError(
                "提示词改写服务地址未配置：请在设置页「提示词改写服务」填写 base_url，"
                "或在 .env 配置 VC_DESIGN_AGENT__BASE_URL"
            )
