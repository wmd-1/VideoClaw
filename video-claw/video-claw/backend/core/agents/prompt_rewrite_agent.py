# -*- coding: utf-8 -*-
"""
原生 H3 提示词改写阶段 Agent（prompt_rewrite）。

数据源：本地 LLM 调用（H3 改写知识资产内嵌于 prompts/prompt_rewrite/，
来源与hash锁定见 references/VERSION）。外部 Design Agent Platform 接入已废弃。

核心机制（决策文档 §5.2/§5.6 实现约束）：
- mode resolver（纯函数）：video_generation_mode + 素材角色 → T2VA/I2VA/L2VA/FL2VA/Ref2VA；
  与 models/custom_video.py::_task_for_input 的传输层 task（t2va/fl2va/ref2va）满足两级契约。
- 每分镜正常路径 1 次 LLM 调用；字段级校验失败带违规清单回喂重试，至多 2 次（有界，非 Agent Loop）。
- 质量四件套：A 跨分镜上下文链（五级优先级，continuity 不得覆盖权威事实）、
  B 角色/场景知识注入、C VLM 参考图 grounding（独立开关 + hash 缓存，失败回退保留模式）、
  D 字段级结构校验器。

介入契约（intervention）：
- {"regenerate_items": [segment_id, ...]}：后台单条重生成；
- {"revise_items": [{"id": segment_id, "instruction": "..."}, ...]}：按用户修改意见修订
  （上一版提示词作为「用户修改」优先级上下文携带）。
"""

import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from core.agents.base_agent import AgentInterface
from prompts.loader import load_prompt

logger = logging.getLogger(__name__)

# MiniMax H3 支持的视频时长范围（秒）；超出夹取到边界
H3_DURATION_MIN = 4
H3_DURATION_MAX = 15
H3_DURATION_DEFAULT = 8

# 校验失败重试上限（含首次共 1 + MAX 次调用）
MAX_VALIDATE_RETRIES = 2

# 指南与模板文件（相对 backend 运行目录）
_REFERENCES_DIR = os.path.join("prompts", "prompt_rewrite", "references")
_BASE_GUIDE_FILE = os.path.join(_REFERENCES_DIR, "base-en.txt")
_REF_GUIDE_FILE = os.path.join(_REFERENCES_DIR, "ref-en.txt")

# H3 输出结构字段（决策文档 §5.6 D：字段存在且顺序正确）
BASE_MODES = {"T2VA", "I2VA", "L2VA", "FL2VA"}
BASE_FIELDS = ("integrated_multimodal_description", "overall_soundscape", "non_diegetic_music")
REF_FIELDS = (
    "subject_definitions", "summary", "retention_analysis",
    "detailed_description", "overall_soundscape", "non_diegetic_music",
)

# 抽象词黑名单（官方指南明令禁止的抽象修饰词）
ABSTRACT_WORDS = (
    "cinematic", "beautiful", "stunning", "breathtaking", "gorgeous",
    "amazing", "spectacular", "masterpiece", "epic",
)

_CODE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```", re.DOTALL)
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
# 引号剔除仅用双引号/直角引号类；英文单引号撇号（如 cat's）不参与配对，避免误删大段正文
_QUOTED_RE = re.compile(r"[\"“”「」『』][^\"“”「」『』]*[\"“”「」『』]")
_LABEL_RE = re.compile(r"<(?:Picture|Video|Audio)\s+\d+>")
_GROUNDING_PROMPT = (
    "请客观描述这张图片的实际画面内容（主体外观、环境、构图、色调与风格），"
    "只描述画面中真实可见的内容，不要写视频生成指令，不要给创作建议。输出一段简洁的中文描述。"
)


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
    """从 LLM 回复中提取最终提示词：优先取 markdown 代码块，否则整段使用。"""
    text = str(assistant_text or "").strip()
    if not text:
        return ""
    matches = _CODE_FENCE_RE.findall(text)
    if matches:
        return max(matches, key=len).strip()
    return text


def resolve_h3_mode(video_generation_mode: str, has_first_frame: bool = False,
                    has_last_frame: bool = False, has_reference_set: bool = False) -> str:
    """mode resolver（纯函数，决策文档 §5.2 契约）。

    video_generation_mode 为默认映射依据，输入素材角色为修正依据：
    - reference 模式 + 参考集 → Ref2VA
    - start_end / first_frame + 帧 → FL2VA / I2VA（仅尾帧 → L2VA）
    - 无图像输入 → T2VA

    传输层契约（custom_video.py::_task_for_input）：Ref2VA→ref2va、
    T2VA→t2va、I2VA/L2VA/FL2VA→fl2va（帧语义由 frame_indices/conditions 区分）。
    """
    mode = str(video_generation_mode or "").strip().lower()
    if has_reference_set or mode == "reference":
        return "Ref2VA"
    if has_first_frame and has_last_frame:
        return "FL2VA"
    if has_last_frame:
        return "L2VA"
    if has_first_frame:
        return "I2VA"
    return "T2VA"


def _validate_h3_prompt(text: str, mode: str, duration: int) -> List[str]:
    """字段级结构校验器（纯函数）。返回人类可读的违规清单；空列表 = 通过。"""
    violations: List[str] = []
    if not text or not text.strip():
        return ["改写结果为空"]

    fields = REF_FIELDS if mode == "Ref2VA" else BASE_FIELDS
    positions = []
    for field in fields:
        match = re.search(rf"^\s*{field}\s*:", text, re.MULTILINE)
        if not match:
            violations.append(f"缺少必填字段：{field}")
        else:
            positions.append((field, match.start()))
    # 字段顺序
    ordered = [p for _, p in positions]
    if ordered != sorted(ordered):
        violations.append("字段顺序与指南规定不一致：" + " → ".join(f for f, _ in positions))

    # 时长声明：目标秒数需在文本中以时间/时长形式出现（如 "8 seconds" / "8.00s" / "0.00s-8.00s"）
    if not re.search(rf"\b{duration}(\.\d+)?\s*(s\b|sec\b|second)", text, re.IGNORECASE):
        violations.append(f"未声明与目标一致的总时长（{duration} 秒）")

    # Ref2VA 标签一致性：全文出现的参考标签必须在 subject_definitions 段有定义
    if mode == "Ref2VA":
        sd_match = re.search(
            r"subject_definitions\s*:(.*?)(?=\n\s*summary\s*:)", text, re.DOTALL
        )
        if sd_match:
            defined = set(_LABEL_RE.findall(sd_match.group(1)))
            used = set(_LABEL_RE.findall(text))
            undefined = used - defined
            if undefined:
                violations.append(f"出现未定义的参考标签：{', '.join(sorted(undefined))}")

    # 抽象词黑名单
    lowered = text.lower()
    hit = [w for w in ABSTRACT_WORDS if re.search(rf"\b{w}\b", lowered)]
    if hit:
        violations.append(f"使用了禁用的抽象词：{', '.join(hit)}")

    # 英文正文（剔除引号内对白/歌词/画面文字后，CJK 占比 > 30% 判不合规）
    stripped = _QUOTED_RE.sub("", text)
    if stripped:
        cjk_count = len(_CJK_RE.findall(stripped))
        if cjk_count / max(len(stripped), 1) > 0.30:
            violations.append("改写正文应为英文（对话、歌词与画面文字可保留原语言）")

    return violations


def _continuity_from_prompt(text: str, mode: str) -> str:
    """从改写结果提取主体与风格连续性摘要（供后续分镜注入；失败返回空串降级）。"""
    if not text:
        return ""
    if mode == "Ref2VA":
        match = re.search(r"subject_definitions\s*:(.*?)(?=\n\s*summary\s*:)", text, re.DOTALL)
        segment = match.group(1).strip() if match else text
    else:
        match = re.search(
            r"integrated_multimodal_description\s*:(.*?)(?=\n\s*overall_soundscape\s*:)",
            text, re.DOTALL,
        )
        segment = match.group(1).strip() if match else text
    sentences = re.split(r"(?<=[.!?])\s+", segment)
    summary = " ".join(sentences[:2]).strip()
    return summary[:240]


def _file_quick_hash(path: str) -> str:
    """图像内容指纹（前 1MB sha256）：grounding 缓存 key 的一部分。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read(1024 * 1024))
    return digest.hexdigest()[:16]


class PromptRewriteAgent(AgentInterface):
    """提示词改写：分镜 → MiniMax H3 规范提示词（原生 LLM 调用，知识资产内嵌）"""

    def __init__(self):
        super().__init__(name="prompt_rewrite")
        # grounding 缓存：(abspath, content_hash) → VLM 客观描述（会话级，不落盘）
        self._grounding_cache: Dict[Tuple[str, str], str] = {}
        self._llm = None
        self._vlm = None

    # ─── 输入收集 ───

    @staticmethod
    def _collect_segments(storyboard_artifact: Dict) -> List[Dict[str, Any]]:
        """从 storyboard 产物收集分镜条目（id/描述/时长/角色），保持 segment 顺序。"""
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
                    "characters": [str(c) for c in (seg.get("characters") or []) if c],
                    "setting": str(seg.get("setting") or "").strip(),
                })
        segments.sort(key=lambda x: x["id"])
        return segments

    # ─── 权威事实（B：角色与场景知识注入） ───

    @staticmethod
    def _authoritative_context(segment: Dict[str, Any], character_artifact: Optional[Dict]) -> str:
        """提取该分镜命中的角色外观描述与场景设定（五级优先级第 1 级）。"""
        lines: List[str] = []
        characters = character_artifact.get("characters", []) if isinstance(character_artifact, dict) else []
        for name in segment.get("characters", []):
            entry = next(
                (c for c in characters if isinstance(c, dict)
                 and str(c.get("name") or c.get("id") or "").strip() == name),
                None,
            )
            desc = str(entry.get("description") or "").strip() if entry else ""
            lines.append(f"- {name}：{desc}" if desc else f"- {name}")
        if segment.get("setting"):
            lines.append(f"- 场景：{segment['setting']}")
        if not lines:
            return ""
        return "【权威事实·角色与场景设定（最高优先级，冲突时以此为准）】\n" + "\n".join(lines) + "\n"

    # ─── VLM grounding（C：独立开关 + hash 缓存 + 正交回退） ───

    def _grounding_describe(self, image_paths: List[str], vlm_model: str) -> Tuple[str, List[str]]:
        """对参考图生成客观画面描述。返回 (描述文本或空串, 命中hash列表)。失败回退空串（模式不变）。"""
        if not Config.H3_REWRITE_GROUNDING_ENABLED or not image_paths:
            return "", []
        descriptions: List[str] = []
        hashes: List[str] = []
        for path in image_paths:
            if not path or not os.path.exists(path):
                continue
            try:
                abspath = os.path.abspath(path)
                content_hash = _file_quick_hash(abspath)
                cached = self._grounding_cache.get((abspath, content_hash))
                if cached is not None:
                    # 缓存命中（含历史失败缓存的空描述）：同图不重复调 VLM
                    if cached:
                        descriptions.append(cached)
                        hashes.append(content_hash)
                    continue
                if self._vlm is None:
                    from models.vlm_client import VLM
                    self._vlm = VLM()
                result = self._vlm.query(_GROUNDING_PROMPT, image_paths=[abspath], model=vlm_model)
                description = str(result or "").strip()
                # 失败/空描述同样缓存，避免同图反复重试 VLM；不记 hash（无有效描述）
                self._grounding_cache[(abspath, content_hash)] = description
                if description:
                    descriptions.append(description)
                    hashes.append(content_hash)
            except Exception as exc:  # noqa: BLE001 - grounding 失败回退文本输入，不阻塞
                logger.warning("prompt_rewrite: 参考图 %s grounding 失败（回退纯文本）：%s", path, exc)
        return "\n".join(descriptions), hashes

    def _grounding_images(self, segment: Dict[str, Any], segments: List[Dict[str, Any]],
                          scene_map: Dict[str, Dict], mode: str) -> List[str]:
        """按模式取 grounding 图：首帧 = 本分镜选中参考图；FL2VA 追加下一分镜选中图作尾帧。"""
        scene = scene_map.get(segment["id"]) or {}
        selected = str(scene.get("selected") or "").strip()
        images = [selected] if selected else []
        if mode == "FL2VA":
            idx = next((i for i, s in enumerate(segments) if s["id"] == segment["id"]), -1)
            if 0 <= idx < len(segments) - 1:
                next_scene = scene_map.get(segments[idx + 1]["id"]) or {}
                next_selected = str(next_scene.get("selected") or "").strip()
                if next_selected:
                    images.append(next_selected)
        return images

    # ─── 请求组装（五级优先级分节） ───

    @staticmethod
    def _render_sections(authoritative: str, shot_content: str, user_section: str,
                         continuity_summaries: List[str], grounding_desc: str) -> Dict[str, str]:
        sections = {
            "authoritative_section": authoritative,
            "current_section": f"【当前分镜】\n{shot_content}\n" if shot_content else "",
            "user_section": user_section,
            "continuity_section": "",
            "grounding_section": "",
        }
        if continuity_summaries:
            joined = "\n".join(f"- {s}" for s in continuity_summaries)
            sections["continuity_section"] = (
                "【连续性摘要（辅助信息，非事实来源；与权威事实或当前分镜冲突时必须忽略）】\n"
                f"{joined}\n"
            )
        if grounding_desc:
            sections["grounding_section"] = (
                "【参考图/首帧实际内容（客观画面描述）】\n"
                f"{grounding_desc}\n"
            )
        return sections

    def _build_request_text(self, segment: Dict[str, Any], mode: str, sections: Dict[str, str]) -> str:
        template = load_prompt("prompt_rewrite", "rewrite", "zh")
        try:
            return template.format(
                input_mode=mode,
                duration=segment["duration"],
                segment_id=segment["id"],
                shot_content=segment["shot_content"],
                **sections,
            )
        except KeyError as exc:
            # 模板与代码不同步时的排障信息（Handoff 3 缺陷 A）：缺失 key + 模板名 + 分镜 id
            missing = str(exc.args[0]) if exc.args else "?"
            logger.error(
                "prompt_rewrite 模板占位符缺少对应 format 参数：missing key=%r, template=%s, segment=%s",
                missing, "prompt_rewrite/rewrite_zh.txt", segment.get("id"),
            )
            raise ValueError(
                f"提示词改写模板缺少占位符参数 {missing!r}（模板与代码版本不同步，请重启后端或更新模板）"
            ) from exc

    @staticmethod
    def _load_guide(mode: str) -> str:
        path = _REF_GUIDE_FILE if mode == "Ref2VA" else _BASE_GUIDE_FILE
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    # ─── LLM 调用（正常 1 次 + 校验失败回喂重试 ≤2） ───

    def _query_llm(self, system_prompt: str, guide: str, request_text: str, llm_model: str) -> str:
        if self._llm is None:
            from models.llm_client import LLM
            self._llm = LLM()
        full_prompt = (
            f"{system_prompt}\n\n"
            f"===== 改写指南（严格遵循）=====\n{guide}\n\n"
            f"===== 本次请求 =====\n{request_text}"
        )
        return self._llm.query(full_prompt, model=llm_model, safe_content=False)

    async def _rewrite_one(self, segment: Dict[str, Any], mode: str, system_prompt: str, guide: str,
                           llm_model: str, sections: Dict[str, str]) -> Tuple[str, List[str], str]:
        """单分镜改写：1 次正常调用 + 校验失败带违规清单回喂重试 ≤2 次。
        返回 (提示词, 违规清单, 最后一次被拒输出)。"""
        base_request = self._build_request_text(segment, mode, sections)
        violations: List[str] = []
        last_output = ""
        for _ in range(1 + MAX_VALIDATE_RETRIES):
            self._check_cancel()
            request_text = base_request
            if violations:
                fix_hint = ""
                if any("抽象词" in v for v in violations):
                    fix_hint += (
                        "\n- 抽象词必须完全移除（含 'cinematic'）：改用具体的镜头运动、机位、"
                        "光线与材质描述（例如 'slow tracking shot at eye level, sodium-vapor lamps reflecting on wet asphalt'）。"
                    )
                if any("时长" in v for v in violations):
                    fix_hint += (
                        "\n- 必须在描述中显式写出总时长（如 '8 seconds' 或 '0.00s-8.00s' 的时间轴）。"
                    )
                request_text = (
                    f"{base_request}\n\n"
                    "【上一次输出未通过校验，违规清单如下，请修正后重新输出完整提示词】\n"
                    + "\n".join(f"- {v}" for v in violations)
                    + fix_hint
                )
            assistant_text = await asyncio.to_thread(
                self._query_llm, system_prompt, guide, request_text, llm_model
            )
            rewritten = _extract_prompt(assistant_text)
            last_output = rewritten or last_output
            violations = _validate_h3_prompt(rewritten, mode, segment["duration"])
            if rewritten and not violations:
                return rewritten, [], ""
        return "", violations or ["改写结果为空"], last_output

    # ─── 条目构造 ───

    @staticmethod
    def _make_item(segment: Dict[str, Any], rewritten_prompt: str, mode: str,
                   grounding_meta: Optional[Dict] = None,
                   prev_item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "id": segment["id"],
            "name": segment["name"],
            "index": segment["index"],
            "episode": segment.get("episode"),
            "original_prompt": segment["shot_content"],
            "rewritten_prompt": rewritten_prompt,
            "input_mode": mode,
            "duration": segment["duration"],
            "status": "done" if rewritten_prompt else "failed",
            "selected": (prev_item or {}).get("selected", ""),
            # 显式浅拷贝：避免与 prev_item 共享 versions 列表引用（修订流程的
            # superseded 版本记录依赖此隔离，防止相互污染历史）
            "versions": list((prev_item or {}).get("versions") or []),
        }
        if grounding_meta is not None:
            item["grounding"] = grounding_meta
        if not rewritten_prompt:
            item["error"] = "改写结果为空或未通过结构校验"
        return item

    @staticmethod
    def _append_version(item: Dict[str, Any], content: str, source: str) -> None:
        versions = item.setdefault("versions", [])
        if not isinstance(versions, list):
            versions = []
            item["versions"] = versions
        versions.append({"content": content, "source": source, "created_at": datetime.now().isoformat()})

    # ─── 核心执行 ───

    async def _rewrite_segments(self, segments: List[Dict[str, Any]], input_data: Dict,
                                prev_items_by_id: Dict[str, Dict[str, Any]],
                                user_sections: Optional[Dict[str, str]] = None,
                                continuity_seeds: Optional[Dict[str, str]] = None) -> Dict[str, Dict[str, Any]]:
        """逐分镜串行改写（continuity 链）；条目级失败隔离，实时透出条目完成事件。"""
        session_meta = self._session_meta(input_data)
        llm_model = (
            str(Config.H3_REWRITE_LLM_MODEL or "").strip()
            or str(input_data.get("llm_model") or session_meta.get("llm_model") or "").strip()
            or str(Config.LLM_MODEL or "").strip()
        )
        vlm_model = (
            str(Config.H3_REWRITE_VLM_MODEL or "").strip()
            or str(input_data.get("vlm_model") or session_meta.get("vlm_model") or "").strip()
            or str(Config.VLM_MODEL or "").strip()
        )
        video_generation_mode = str(
            input_data.get("video_generation_mode") or session_meta.get("video_generation_mode") or "first_frame"
        ).strip().lower()
        artifacts = self._session_artifacts(input_data)
        character_artifact = artifacts.get("character_design", {})
        scene_map = {
            s.get("id"): s for s in (artifacts.get("reference_generation", {}).get("scenes", []) or [])
            if isinstance(s, dict) and s.get("id")
        }

        system_prompt = load_prompt("prompt_rewrite", "system", "zh")

        # continuity 链：串行累积各分镜的主体/风格摘要
        continuity_summaries: List[str] = list(continuity_seeds or [])
        results: Dict[str, Dict[str, Any]] = {}
        total = len(segments)
        for i, segment in enumerate(segments):
            self._check_cancel()
            prev_item = prev_items_by_id.get(segment["id"])
            # mode resolver：素材角色来自参考图阶段产物（start_end_frame 的尾帧 = 下一分镜选中图，
            # 与 video_agent 的首尾帧取图逻辑一致；尾帧缺失时 resolver 落 I2VA，与传输层回退一致）
            selected = str((scene_map.get(segment["id"]) or {}).get("selected") or "").strip()
            next_selected = ""
            if video_generation_mode == "start_end_frame" and i < len(segments) - 1:
                next_selected = str((scene_map.get(segments[i + 1]["id"]) or {}).get("selected") or "").strip()
            mode = resolve_h3_mode(
                video_generation_mode,
                has_first_frame=bool(selected),
                has_last_frame=bool(next_selected),
                has_reference_set=(video_generation_mode == "reference"),
            )
            guide = await asyncio.to_thread(self._load_guide, mode)

            # C：VLM grounding（独立开关；失败回退文本输入，模式不变）
            if mode in ("I2VA", "FL2VA", "Ref2VA"):
                grounding_desc, grounding_hashes = await asyncio.to_thread(
                    self._grounding_describe,
                    self._grounding_images(segment, segments, scene_map, mode),
                    vlm_model,
                )
            else:
                grounding_desc, grounding_hashes = "", []
            if grounding_desc or grounding_hashes:
                grounding_meta = {"image_hash": grounding_hashes[0] if grounding_hashes else "",
                                  "described": bool(grounding_desc)}
            elif mode in ("I2VA", "FL2VA", "Ref2VA"):
                grounding_meta = {"described": False, "text_only": True}
            else:
                grounding_meta = None

            sections = self._render_sections(
                authoritative=self._authoritative_context(segment, character_artifact),
                shot_content=segment["shot_content"],
                user_section=(user_sections or {}).get(segment["id"], ""),
                continuity_summaries=continuity_summaries,
                grounding_desc=grounding_desc,
            )

            item = self._make_item(segment, "", mode, grounding_meta, prev_item)
            try:
                rewritten, violations, rejected = await self._rewrite_one(
                    segment, mode, system_prompt, guide, llm_model, sections
                )
                if rewritten:
                    item = self._make_item(segment, rewritten, mode, grounding_meta, prev_item)
                    self._append_version(item, rewritten, source="agent")
                    item["continuity"] = _continuity_from_prompt(rewritten, mode)
                    continuity_summaries.append(item["continuity"])
                else:
                    item = self._make_item(segment, "", mode, grounding_meta, prev_item)
                    item["status"] = "failed"
                    item["error"] = "结构校验未通过：" + "；".join(violations)
                    if rejected:
                        item["rejected_output"] = rejected  # 保留最后被拒输出供诊断/人工采用
            except Exception as exc:  # noqa: BLE001 - 单条目失败不阻塞其他条目
                logger.warning("prompt_rewrite: 分镜 %s 改写失败: %s", segment["id"], exc)
                item = self._make_item(segment, "", mode, grounding_meta, prev_item)
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
        # 前置校验：未启用时明确失败（不静默回退）
        self.check_enabled()
        input_data = self._merge_session_params(input_data if isinstance(input_data, dict) else {})
        artifacts = self._session_artifacts(input_data)
        storyboard_artifact = self._session_artifact(input_data, "storyboard")
        prev_artifact = artifacts.get("prompt_rewrite") if isinstance(artifacts.get("prompt_rewrite"), dict) else {}
        prev_items = prev_artifact.get("items", []) if isinstance(prev_artifact.get("items"), list) else []
        prev_items_by_id = {item.get("id"): item for item in prev_items if isinstance(item, dict) and item.get("id")}

        segments = self._collect_segments(storyboard_artifact)
        if not segments:
            raise ValueError("提示词改写阶段缺少分镜数据：请先完成分镜设计阶段")

        # ── 介入：按用户修改意见修订（上一版进「用户修改」优先级区） ──
        if isinstance(intervention, dict) and isinstance(intervention.get("revise_items"), list) and intervention["revise_items"]:
            return await self._process_revisions(intervention["revise_items"], segments, prev_items_by_id, input_data)

        # ── 介入：后台单条重生成（只处理指定条目，由编排器合并） ──
        regenerate_items: List[str] = []
        if isinstance(intervention, dict) and isinstance(intervention.get("regenerate_items"), list):
            regenerate_items = [str(x) for x in intervention["regenerate_items"] if x]
        if regenerate_items:
            target_segments = [seg for seg in segments if seg["id"] in set(regenerate_items)]
            missing = [sid for sid in regenerate_items if sid not in {seg["id"] for seg in target_segments}]
            if missing:
                raise ValueError(f"待重生成的分镜不存在：{', '.join(missing)}")
            # 单条重生成不携带 continuity 链（独立重写），但保留权威事实与 grounding
            results = await self._rewrite_segments(target_segments, input_data, prev_items_by_id)
            items = [results[sid] for sid in regenerate_items if sid in results]
            return {"payload": {"items": items}, "requires_intervention": True}

        # ── 首次 / 全量执行 ──
        self._report_progress("提示词改写", "开始改写分镜提示词...", 2)
        results = await self._rewrite_segments(segments, input_data, prev_items_by_id)

        items = [results[seg["id"]] for seg in segments if seg["id"] in results]
        done_count = sum(1 for item in items if item["status"] == "done")
        self._report_progress("提示词改写", f"改写完成（{done_count}/{len(items)} 成功）", 100)
        return {
            "payload": {"items": items},
            "requires_intervention": True,
            "stage_completed": True,
        }

    async def _process_revisions(self, revise_items: List[Dict], segments: List[Dict[str, Any]],
                                 prev_items_by_id: Dict[str, Dict[str, Any]], input_data: Dict) -> Dict:
        seg_by_id = {seg["id"]: seg for seg in segments}
        targets: List[Dict[str, Any]] = []
        user_sections: Dict[str, str] = {}
        for revision in revise_items:
            if not isinstance(revision, dict):
                continue
            seg_id = str(revision.get("id") or "")
            instruction = str(revision.get("instruction") or "").strip()
            seg = seg_by_id.get(seg_id)
            prev_item = prev_items_by_id.get(seg_id)
            if not seg or not prev_item or not instruction:
                continue
            targets.append(seg)
            prev_text = str(prev_item.get("rewritten_prompt") or "").strip()
            user_sections[seg_id] = (
                f"【用户修改意见（优先级高于分镜描述，低于权威事实）】\n{instruction}\n"
                + (f"上一版提示词（在此基础上修订）：\n{prev_text}\n" if prev_text else "")
            )
        if not targets:
            return {"payload": {"items": []}, "requires_intervention": True}

        results = await self._rewrite_segments(
            targets, input_data, prev_items_by_id, user_sections=user_sections
        )
        items = []
        for seg in targets:
            item = results.get(seg["id"])
            if item and item["status"] == "done":
                prev_item = prev_items_by_id.get(seg["id"]) or {}
                old_text = str(prev_item.get("rewritten_prompt") or "")
                if old_text:
                    self._append_version(item, old_text, source="superseded")
            items.append(item)
        return {"payload": {"items": [i for i in items if i]}, "requires_intervention": True}

    @staticmethod
    def check_enabled() -> None:
        """阶段执行前置校验：未启用时抛出明确错误（由编排器/路由层调用）。"""
        if not Config.H3_REWRITE_ENABLED:
            raise ValueError(
                "提示词改写阶段未启用：请在设置页「H3 提示词改写」开启 enable，"
                "或在 .env 配置 VC_H3_REWRITE__ENABLE=true"
            )
