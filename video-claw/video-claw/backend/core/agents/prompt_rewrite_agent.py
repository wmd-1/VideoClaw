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
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from config import Config
from core.agents.base_agent import AgentInterface
from core.agents.video_agent import VideoDirectorAgent
from core.grounding_cache import content_sha256, grounding_cache
from core.h3_modes import authority_facts as _shared_authority_facts
from core.h3_modes import collect_segments as _shared_collect_segments
from core.h3_modes import fingerprint_parts as _shared_fingerprint_parts
from core.h3_modes import next_in_order as _shared_next_in_order
from core.h3_modes import resolve_grounding_model, resolve_h3_mode, resolve_style_key
from core.h3_modes import setting_description as _shared_setting_description
from core.h3_modes import setting_entry as _shared_setting_entry
from core.observability import call_context, content_version, record_call
from prompts.loader import load_prompt

logger = logging.getLogger(__name__)

# 校验失败重试上限（含首次共 1 + MAX 次调用）
MAX_VALIDATE_RETRIES = 2

# 条目完成事件里改写正文的传输上限（**仅作用于增量事件，不影响也不落入服务端持久化产物**）。
# 这是集中定义的工程默认值：本仓库尚无真实推理输出的长度分布样本（历史会话中 prompt_rewrite 条目为 0），
# 未经真实分布验证，不得当成长期契约；超限部分由 truncated 显式标记，完整正文以阶段产物为权威。
EVENT_BODY_LIMIT = 8000
# 校验失败回喂时携带的被拒草稿上限；按字段分节分配，避免从头截断丢掉尾部字段。
DRAFT_LIMIT = 6000
# 草稿头部（关键帧对齐指令等非字段内容）在回喂中的保留上限。
DRAFT_HEADER_LIMIT = 600
# 失败条目的被拒输出仅用于诊断；限长后随产物返回，避免轮询/产物被超长输出放大。
REJECTED_OUTPUT_LIMIT = 2000

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
# D2：grounding 策略版本（含语言）；提示词变化时缓存自动失效
_GROUNDING_POLICY_VERSION = content_version(_GROUNDING_PROMPT)


def _event_item_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    """条目完成事件的增量载荷：让前端在阶段完成前就能渲染该条。

    只负责“事件多带哪些字段”，不改变服务端产物合并与状态归一语义（shell 仍不落正文）。
    超长正文截断并置 truncated，完整正文随阶段产物覆盖。
    """
    body = str(item.get("rewritten_prompt") or "")
    summary: Dict[str, Any] = {
        "name": item.get("name") or item.get("id"),
        "index": item.get("index"),
        "episode": item.get("episode"),
        "original_prompt": item.get("original_prompt") or "",
        "input_mode": item.get("input_mode") or "",
        "duration": item.get("duration"),
        "continuity": item.get("continuity") or "",
        "truncated": len(body) > EVENT_BODY_LIMIT,
    }
    if item.get("grounding") is not None:
        summary["grounding"] = item["grounding"]
    if body:
        summary["rewritten_prompt"] = body[:EVENT_BODY_LIMIT]
    return summary


def _extract_prompt(assistant_text: str) -> str:
    """从 LLM 回复中提取最终提示词：优先取 markdown 代码块，否则整段使用。"""
    text = str(assistant_text or "").strip()
    if not text:
        return ""
    matches = _CODE_FENCE_RE.findall(text)
    if matches:
        return max(matches, key=len).strip()
    return text


def _seconds_from_mmss(mm: str, ss: str) -> float:
    return int(mm) * 60 + float(ss)


def _normalize_keyframe_spacing(text: str, mode: str) -> str:
    """关键帧模式：对齐声明与核心字段之间补一个空行。

    实测真实模型稳定输出「对齐句\\nintegrated_multimodal_description:」，不会遵守空行要求；
    这是纯格式问题，不应让整个条目失败。规范化只补空行，不改任何正文内容。
    """
    if mode not in ("I2VA", "FL2VA", "L2VA") or not text:
        return text
    lines = text.splitlines()
    if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
        return text
    first = lines[0].strip().lower()
    if "aligns with" not in first and "into the target video" not in first:
        return text
    fields = "|".join(BASE_FIELDS)
    if re.match(rf"^\s*(?:{fields})\s*:", lines[1]):
        lines.insert(1, "")
        return "\n".join(lines)
    return text


def _validate_keyframe_alignment(text: str, mode: str, duration: int) -> List[str]:
    """关键帧模式的对齐声明、S.SS 格式与允许对齐点（官方 base-en L19/L25/L31/L34）。"""
    violations: List[str] = []
    if mode not in ("I2VA", "FL2VA", "L2VA"):
        return violations
    lines = text.strip().splitlines()
    first = lines[0].strip() if lines else ""
    second = lines[1].strip() if len(lines) > 1 else ""

    if mode == "I2VA":
        if not re.search(r"at\s+0\.00\s+seconds?\s+into\s+the\s+target\s+video", first, re.IGNORECASE) \
                or "<Picture 1>" not in first:
            violations.append(
                "缺少 I2VA 首帧对齐声明（首行须为 at 0.00 seconds into the target video 且含 <Picture 1>）"
            )
    elif "aligns with" not in first.lower():
        violations.append(f"缺少 {mode} 对齐声明（首行须为参考图与目标视频的对齐说明）")

    if len(lines) > 1 and second:
        violations.append("对齐声明后缺少空行（须与核心字段之间空一行）")

    # 对齐时间只从对齐声明首行提取：正文里的 -second mark 不属于对齐声明，避免误伤。
    marks = re.findall(r"(\d+(?:\.\d+)?)-second\s+mark", first, re.IGNORECASE)
    bad_formats = [m for m in marks if not re.fullmatch(r"\d+\.\d{2}", m)]
    if bad_formats:
        violations.append("对齐时间未使用官方 S.SS 两位小数格式：" + ", ".join(bad_formats))

    normalized = {m for m in marks if re.fullmatch(r"\d+\.\d{2}", m)}
    if mode == "L2VA" and f"{duration}.00" not in normalized:
        violations.append(f"未声明尾帧对齐于目标时长（{duration}.00-second mark）")
    if mode == "FL2VA" and not {"0.00", f"{duration}.00"}.issubset(normalized):
        violations.append(f"未同时声明首帧 0.00 与尾帧 {duration}.00 的对齐")
    allowed = (
        {"0.00", f"{duration}.00"} if mode == "FL2VA"
        else {"0.00"} if mode == "I2VA"
        else {f"{duration}.00"}
    )
    extra = normalized - allowed
    if extra:
        violations.append("对齐时间点与官方允许值不一致：" + ", ".join(sorted(extra)))
    return violations


def _validate_shot_timeline(text: str, duration: int) -> List[str]:
    """镜头时间轴：首镜头无时间戳、cut time 严格递增、时间点不越界（base-en L88）。"""
    violations: List[str] = []
    shots = re.findall(r"\[Shot\s+(\d+)\](.*?)(?=\[Shot\s+\d+\]|$)", text, re.DOTALL)
    cut_times: List[float] = []
    for number, body in shots:
        if int(number) == 1:
            if re.search(r"At\s+\d{2}:\d{2}\.\d{3}", body, re.IGNORECASE):
                violations.append("首个镜头不得添加时间戳（[Shot 1] 内出现 At MM:SS.mmm）")
            continue
        match = re.search(r"At\s+(\d{2}):(\d{2}\.\d{3})", body, re.IGNORECASE)
        if match:
            cut_times.append(_seconds_from_mmss(match.group(1), match.group(2)))
        else:
            violations.append(f"[Shot {number}] 缺少严格递增的 cut time（At MM:SS.mmm）")
    for index in range(1, len(cut_times)):
        if cut_times[index] <= cut_times[index - 1]:
            violations.append("后续镜头的 cut time 必须严格递增")
            break
    for mm, ss in re.findall(r"At\s+(\d{2}):(\d{2}\.\d{3})", text, re.IGNORECASE):
        value = _seconds_from_mmss(mm, ss)
        if value > duration + 1e-6:
            violations.append(f"时间点 {value:.2f}s 超出目标时长（{duration} 秒）")
            break
    for value in re.findall(r"(\d+(?:\.\d+)?)-second\s+mark", text, re.IGNORECASE):
        try:
            number = float(value)
        except ValueError:
            continue
        if number > duration + 1e-6:
            violations.append(f"对齐时间点 {value}s 超出目标时长（{duration} 秒）")
            break
    return violations


def _validate_h3_prompt(text: str, mode: str, duration: int, *,
                        picture_count: Optional[int] = None) -> List[str]:
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
    # 接受 "5 seconds" / "5 secs" / "5.00s" / "5.00-second" / "0.00s-5.00s"；
    # 只允许整数或全零小数，5.5/4.9 等与目标时长不一致的写法仍判违规；
    # 只认 H3 正式写法（5 / 5.00），不接受 05.00s 这类前导零变体
    if not re.search(
        rf"(?<![\d.]){duration}(?:\.0+)?\s*(?:-\s*)?(s\b|secs?\b|seconds?\b)",
        text, re.IGNORECASE,
    ):
        violations.append(f"未声明与目标一致的总时长（{duration} 秒）")

    # R9：未定义标签仅用于 Ref2VA（基础模式没有 subject_definitions 段）
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
    else:
        violations.extend(_validate_keyframe_alignment(text, mode, duration))

    # R5-R7：镜头时间轴与时间点
    violations.extend(_validate_shot_timeline(text, duration))

    # R8：<Picture N> 必须对应实际参考图数量
    if picture_count is not None:
        used_pictures = sorted({int(n) for n in re.findall(r"<Picture\s+(\d+)>", text)})
        invalid = [n for n in used_pictures if n < 1 or n > picture_count]
        if invalid:
            violations.append(
                f"参考图片标签超出实际素材数量（实际 {picture_count} 张）："
                + ", ".join(f"<Picture {n}>" for n in invalid)
            )

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


def _truncate_draft_by_field(text: str, mode: str, limit: int = DRAFT_LIMIT) -> str:
    """按 H3 字段分节截断被拒草稿。

    从头截断会整体丢掉 `overall_soundscape` / `non_diegetic_music` 等尾部字段，
    使"草稿需足够完整"失效；这里保留头部（对齐指令）与每个字段的片段，逐节标记截断。
    """
    if not text or len(text) <= limit:
        return text
    fields = REF_FIELDS if mode == "Ref2VA" else BASE_FIELDS
    positions: List[tuple] = []
    for field in fields:
        match = re.search(rf"^\s*{field}\s*:", text, re.MULTILINE)
        if match:
            positions.append((match.start(), field))
    if not positions:
        return text[:limit].rstrip() + "\n…（草稿已截断）"
    positions.sort()
    header = text[:positions[0][0]].strip()
    if len(header) > DRAFT_HEADER_LIMIT:
        header = header[:DRAFT_HEADER_LIMIT].rstrip() + "\n…（对齐指令已截断）"
    parts: List[str] = [header] if header else []
    per_field = max(200, (limit - len(header)) // len(positions))
    for index, (start, field) in enumerate(positions):
        end = positions[index + 1][0] if index + 1 < len(positions) else len(text)
        chunk = text[start:end].strip()
        if len(chunk) > per_field:
            chunk = chunk[:per_field].rstrip() + f"\n…（{field} 已截断）"
        parts.append(chunk)
    return "\n\n".join(parts)


def _build_revision_section(draft: str, feedback: List[str], mode: str) -> str:
    """修订专用分节：问题清单 + 按字段分节截断后的被拒草稿。"""
    if not draft or not feedback:
        return ""
    issues = "\n".join(f"- {item}" for item in feedback)
    extra = ""
    if any("抽象词" in item for item in feedback):
        extra += (
            "\n- 抽象词必须完全移除（含 'cinematic'）：改用具体的镜头运动、机位、"
            "光线与材质描述（例如 'slow tracking shot at eye level, sodium-vapor lamps reflecting on wet asphalt'）。"
        )
    if any("时长" in item for item in feedback):
        extra += "\n- 必须在描述中显式写出总时长（如 '8 seconds' 或 '0.00s-8.00s' 的时间轴）。"
    return (
        "\n【上一次输出未通过校验，请在草稿基础上仅修正以下问题，其余内容保持不变】\n"
        f"{issues}{extra}\n"
        "【被拒草稿（按字段分节；标注已截断的字段不得凭空补写，只按问题清单修正）】\n"
        f"{_truncate_draft_by_field(draft, mode)}\n"
    )


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """从评审回复中提取第一个 JSON 对象（兼容 Markdown 代码块）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    fenced = _CODE_FENCE_RE.findall(raw)
    candidates = fenced or [raw]
    for candidate in candidates:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            payload = json.loads(candidate[start:end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _issue_messages(quality: Optional[Dict[str, Any]]) -> List[str]:
    """评审问题 → 回喂用的字符串清单（不得把 dict 直接渲染进提示词）。"""
    if not isinstance(quality, dict):
        return []
    messages: List[str] = []
    for issue in quality.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        message = str(issue.get("message") or "").strip()
        if message:
            severity = str(issue.get("severity") or "minor")
            messages.append(f"[{severity}] {message}")
    if not messages and quality.get("verdict") == "needs_revision":
        messages.append(
            f"评审得分 {quality.get('score')} 低于阈值 {quality.get('threshold')}，请提升具体性与一致性"
        )
    return messages


def _mark_quality_applies(quality: Optional[Dict[str, Any]], final_prompt: str) -> None:
    """标注评分是否适用于最终正文；修订后未复评时不得把旧分当作最终结论。"""
    if not isinstance(quality, dict):
        return
    judged = str(quality.get("judged_prompt_hash") or "")
    quality["applies_to_final"] = bool(judged) and judged == content_version(final_prompt)


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
        # D2：负结果仅当前执行复用；成功描述走进程内会话缓存
        self._grounding_negative: set = set()
        self._llm = None
        self._vlm = None

    # ─── 输入收集 ───

    @staticmethod
    def _collect_segments(storyboard_artifact: Dict, model: str = "",
                          session_override: Any = None) -> List[Dict[str, Any]]:
        """从 storyboard 产物收集分镜条目（id/描述/有效时长/角色/场景）。

        时长与归一化由 `core.h3_modes.collect_segments` 单一实现，视频阶段算指纹时
        必须调用同一函数，否则两侧 `shot_content` 取法不同会恒 stale。
        """
        return _shared_collect_segments(storyboard_artifact, model, session_override)

    # ─── 权威事实（B：角色与场景知识注入） ───

    @staticmethod
    def _setting_entry(location: str, settings: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """按稳定 ID 或规范名查找场景条目；与提示词注入/指纹共用。"""
        return _shared_setting_entry(location, settings)

    @staticmethod
    def _setting_description(location: str, settings: List[Dict[str, Any]]) -> str:
        """场景描述（与提示词注入共用同一匹配）。"""
        return _shared_setting_description(location, settings)

    @staticmethod
    def _authoritative_context(segment: Dict[str, Any], character_artifact: Optional[Dict]) -> str:
        """提取该分镜命中的角色外观描述与场景设定（五级优先级第 1 级）。"""
        artifact = character_artifact if isinstance(character_artifact, dict) else {}
        characters = artifact.get("characters", [])
        characters = characters if isinstance(characters, list) else []
        settings = artifact.get("settings", [])
        settings = settings if isinstance(settings, list) else []
        lines: List[str] = []
        for name in segment.get("characters", []):
            entry = next(
                (c for c in characters if isinstance(c, dict)
                 and str(c.get("name") or c.get("id") or "").strip() == name),
                None,
            )
            desc = str(entry.get("description") or "").strip() if entry else ""
            lines.append(f"- {name}：{desc}" if desc else f"- {name}")
        location = str(segment.get("location") or "").strip()
        if location:
            scene_desc = PromptRewriteAgent._setting_description(location, settings)
            lines.append(f"- 场景：{location}：{scene_desc}" if scene_desc else f"- 场景：{location}")
        if not lines:
            return ""
        return "【权威事实·角色与场景设定（最高优先级，冲突时以此为准）】\n" + "\n".join(lines) + "\n"

    # ─── VLM grounding（C：独立开关 + hash 缓存 + 正交回退） ───

    def _grounding_describe(self, image_paths: List[str], vlm_model: str,
                            session_id: str = "") -> Tuple[str, List[str]]:
        """对参考图生成客观画面描述。返回 (带编号的描述文本, 命中hash列表)。

        成功描述进入会话级 grounding 缓存；空/失败仅当前执行负缓存。
        输出按视频入参顺序编号；某图不可读时写占位，编号不位移。
        """
        if not Config.H3_REWRITE_GROUNDING_ENABLED or not image_paths:
            return "", []
        descriptions: List[str] = []
        hashes: List[str] = []
        for index, path in enumerate(image_paths, 1):
            placeholder = f"<Picture {index}>：（本图无可读描述）"
            if not path or not os.path.exists(path):
                descriptions.append(placeholder)
                continue
            try:
                abspath = os.path.abspath(path)
                content_hash = content_sha256(abspath)
                negative_key = (session_id, content_hash, vlm_model, _GROUNDING_POLICY_VERSION)
                if negative_key in self._grounding_negative:
                    descriptions.append(placeholder)
                    continue
                cached = grounding_cache.get(
                    session_id, content_hash, vlm_model, _GROUNDING_POLICY_VERSION,
                )
                if cached:
                    record_call(
                        call_kind="vlm", model=vlm_model, session_id=session_id,
                        prompt_hash=content_version(_GROUNDING_PROMPT),
                        image_count=1, cache_hit=True, cache_key_kind="grounding",
                    )
                    descriptions.append(f"<Picture {index}>：{cached}")
                    hashes.append(content_hash)
                    continue

                def _compute() -> str:
                    if self._vlm is None:
                        from models.vlm_client import VLM
                        self._vlm = VLM()
                    result = self._vlm.query(
                        _GROUNDING_PROMPT, image_paths=[abspath], model=vlm_model,
                    )
                    return str(result or "").strip()

                description = grounding_cache.get_or_compute(
                    session_id, content_hash, vlm_model, _GROUNDING_POLICY_VERSION, _compute,
                )
                if description:
                    descriptions.append(f"<Picture {index}>：{description}")
                    hashes.append(content_hash)
                else:
                    self._grounding_negative.add(negative_key)
                    descriptions.append(placeholder)
            except Exception as exc:  # noqa: BLE001 - grounding 失败回退文本输入，不阻塞
                logger.warning("prompt_rewrite: 参考图 %s grounding 失败（回退纯文本）：%s", path, exc)
                descriptions.append(placeholder)
        if not hashes:
            return "", []
        return "\n".join(descriptions), hashes

    @staticmethod
    def _next_in_order(ordered: List[Dict[str, Any]], seg_id: str) -> Optional[Dict[str, Any]]:
        """完整有序列表中的下一片段（B3：邻接只从全量算，目标集合不参与）。"""
        return _shared_next_in_order(ordered, seg_id)

    @staticmethod
    def _reference_asset_images(segment: Dict[str, Any], artifacts: Dict[str, Any]) -> List[str]:
        """Ref2VA：按消费计划的**同一顺序**（场景→角色）取物化后的选中图。

        不自行按 `selected` 重推或按名称重匹配：回退规则（versions）与去重都由消费
        计划统一，否则 `<Picture N>` 会与视频入参错位。
        """
        from core.consumption import reference_assets_for_segment

        images: List[str] = []
        for _container, _key, asset in reference_assets_for_segment(artifacts, segment):
            path = str(asset.get("selected") or "").strip()
            if path:
                images.append(path)
        return images

    @staticmethod
    def _authority_facts(segment: Dict[str, Any], character_artifact: Optional[Dict]) -> List[Dict[str, Any]]:
        """本分镜的权威事实（**与 `_authoritative_context` 同一匹配**，保证指纹与提示词一致）。"""
        return _shared_authority_facts(segment, character_artifact)

    def _input_fingerprint_parts(self, segment: Dict[str, Any], mode: str,
                                 ordered: List[Dict[str, Any]], scene_map: Dict[str, Dict],
                                 artifacts: Dict[str, Any], input_data: Dict,
                                 session_meta: Dict, *, grounding_model: str = "") -> tuple:
        """返回 (整体指纹, 逐维度哈希)；与视频阶段共用同一实现。"""
        return _shared_fingerprint_parts(
            segment, mode, ordered, scene_map, artifacts,
            style=resolve_style_key(input_data, session_meta),
            grounding_model=grounding_model,
        )

    def _input_fingerprint(self, segment: Dict[str, Any], mode: str,
                           ordered: List[Dict[str, Any]], scene_map: Dict[str, Dict],
                           artifacts: Dict[str, Any], input_data: Dict,
                           session_meta: Dict, *, grounding_model: str = "") -> str:
        """Q1：当次输入指纹（参考集用有序清单，不按名称）。"""
        return self._input_fingerprint_parts(
            segment, mode, ordered, scene_map, artifacts, input_data, session_meta,
            grounding_model=grounding_model,
        )[0]

    def _grounding_images(self, segment: Dict[str, Any], ordered_segments: List[Dict[str, Any]],
                          scene_map: Dict[str, Dict], mode: str,
                          artifacts: Optional[Dict[str, Any]] = None) -> List[str]:
        """参考图清单：grounding 与 `<Picture N>` 数量校验共用同一取法。

        首帧 = 本分镜选中参考图；FL2VA / L2VA 追加下一分镜选中图作尾帧（L2VA 本分镜
        通常无图，尾帧来自下一分镜）。`ordered_segments` 必须是**完整有序分镜**（非本次
        目标子集），否则单条/批量重写会取错邻接（B3）。Ref2VA 直接消费消费计划给出的
        有序参考集（B1）。
        """
        if mode == "Ref2VA":
            return self._reference_asset_images(segment, artifacts or {})
        scene = scene_map.get(segment["id"]) or {}
        selected = str(scene.get("selected") or "").strip()
        images = [selected] if selected else []
        # FL2VA 追加下一分镜尾帧；L2VA 的生产输入是本分镜无图、尾帧来自下一分镜，
        # 同样必须追加，否则清单为空会把官方 <Picture 1> 误判为超量标签。
        if mode in ("FL2VA", "L2VA"):
            nxt = self._next_in_order(ordered_segments, segment["id"])
            if nxt is not None:
                next_scene = scene_map.get(nxt["id"]) or {}
                next_selected = str(next_scene.get("selected") or "").strip()
                if next_selected:
                    images.append(next_selected)
        return images

    # ─── 请求组装（五级优先级分节） ───

    @staticmethod
    def _render_sections(authoritative: str, shot_content: str, user_section: str,
                         continuity_summaries: List[str], grounding_desc: str,
                         revision_section: str = "") -> Dict[str, str]:
        sections = {
            "authoritative_section": authoritative,
            "current_section": f"【当前分镜】\n{shot_content}\n" if shot_content else "",
            "user_section": user_section,
            "continuity_section": "",
            "grounding_section": "",
            "revision_section": revision_section,
        }
        if continuity_summaries:
            joined = "\n".join(f"- {s}" for s in continuity_summaries)
            sections["continuity_section"] = (
                "【连续性摘要（辅助信息，非事实来源；与权威事实或当前分镜冲突时必须忽略）】\n"
                f"{joined}\n"
            )
        if grounding_desc:
            sections["grounding_section"] = (
                "【参考图/首帧实际内容（客观事实；关键帧任务中被固定的首帧/尾帧必须严格一致）】\n"
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

    def _run_judge(self, draft: str, mode: str, duration: int, sections: Dict[str, str],
                   shot_content: str, llm_model: str, revision: int) -> Dict[str, Any]:
        """一次多维度质量评审。任何失败都归一为 unavailable dict，绝不抛出。"""
        model = (
            str(Config.H3_REWRITE_JUDGE_MODEL or "").strip()
            or str(Config.H3_REWRITE_LLM_MODEL or "").strip()
            or str(llm_model or "").strip()
        )
        base = {
            "judged_prompt_hash": content_version(draft),
            "judged_revision": revision,
            "model": model,
        }
        try:
            template = load_prompt("prompt_rewrite", "judge", "zh")
            prompt = template.format(
                input_mode=mode,
                duration=duration,
                shot_content=shot_content,
                authoritative_section=sections.get("authoritative_section", ""),
                grounding_section=sections.get("grounding_section", ""),
                continuity_section=sections.get("continuity_section", ""),
                draft_prompt=draft,
            )
            if self._llm is None:
                from models.llm_client import LLM
                self._llm = LLM()
            with call_context(
                operation="prompt_rewrite", template_name="judge_zh",
                template_version=content_version(template),
            ):
                raw = self._llm.query(prompt, model=model, safe_content=False)
            payload = _extract_json_object(raw)
            if not isinstance(payload, dict):
                return {**base, "verdict": "unavailable", "error": "评审输出无法解析为 JSON"}
            try:
                score = max(1, min(5, int(payload.get("score"))))
            except (TypeError, ValueError):
                return {**base, "verdict": "unavailable", "error": "评审输出缺少有效 score"}
            dimensions = payload.get("dimensions")
            dimensions = dimensions if isinstance(dimensions, dict) else {}
            issues: List[Dict[str, str]] = []
            for issue in payload.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                message = str(issue.get("message") or "").strip()
                if not message:
                    continue
                issues.append({
                    "dimension": str(issue.get("dimension") or ""),
                    "severity": str(issue.get("severity") or "minor"),
                    "message": message,
                })
            threshold = max(1, min(5, int(Config.H3_REWRITE_JUDGE_THRESHOLD or 3)))
            needs_revision = (
                bool(payload.get("needs_revision"))
                or score < threshold
                or any(item["severity"] == "blocker" for item in issues)
            )
            return {
                **base,
                "verdict": "needs_revision" if needs_revision else "pass",
                "score": score,
                "threshold": threshold,
                "dimensions": dimensions,
                "issues": issues,
                "revision_attempted": False,
            }
        except Exception as exc:  # noqa: BLE001 - 评审失败只降级，不阻塞改写
            logger.warning("prompt_rewrite: 质量评审失败（降级 unavailable）：%s", exc)
            return {**base, "verdict": "unavailable", "error": str(exc)[:200]}

    async def _rewrite_one(self, segment: Dict[str, Any], mode: str, system_prompt: str, guide: str,
                           llm_model: str, sections: Dict[str, str],
                           picture_count: Optional[int] = None) -> Tuple[str, List[str], str, Optional[Dict]]:
        """单分镜改写：1 次正常调用 + 校验失败带草稿与违规清单回喂重试 ≤2 次。

        返回 (提示词, 违规清单, 最后一次被拒输出, quality)；
        评审关闭时 quality 恒为 None，调用次数与既有行为一致。
        """
        violations: List[str] = []
        last_output = ""
        draft = ""
        last_valid = ""
        prev_feedback: List[str] = []
        judged = False
        quality: Optional[Dict[str, Any]] = None
        judge_enabled = bool(Config.H3_REWRITE_JUDGE_ENABLED)
        for attempt in range(1 + MAX_VALIDATE_RETRIES):
            self._check_cancel()
            # 收敛保护：连续两轮问题清单相同说明带草稿修订未生效，回退为完整重写。
            revision_section = ""
            if draft and violations and violations != prev_feedback:
                revision_section = _build_revision_section(draft, violations, mode)
            request_text = self._build_request_text(
                segment, mode, {**sections, "revision_section": revision_section},
            )
            assistant_text = await asyncio.to_thread(
                self._query_llm, system_prompt, guide, request_text, llm_model
            )
            rewritten = _normalize_keyframe_spacing(_extract_prompt(assistant_text), mode)
            last_output = rewritten or last_output
            prev_feedback = violations
            violations = _validate_h3_prompt(
                rewritten, mode, segment["duration"], picture_count=picture_count,
            )
            draft = rewritten or draft
            if rewritten and not violations:
                last_valid = rewritten
                if judge_enabled and not judged:
                    judged = True
                    quality = await asyncio.to_thread(
                        self._run_judge, rewritten, mode, segment["duration"], sections,
                        segment["shot_content"], llm_model, attempt + 1,
                    )
                    if quality.get("verdict") == "needs_revision" and attempt < MAX_VALIDATE_RETRIES:
                        quality["revision_attempted"] = True
                        violations = _issue_messages(quality)
                        record_call(
                            event_type="quality_revision", call_kind="text",
                            model=str(quality.get("model") or llm_model),
                            attempt=attempt + 1, retry_reason="quality", status="ok",
                        )
                        continue
                _mark_quality_applies(quality, rewritten)
                return rewritten, [], "", quality
            if violations:
                record_call(
                    event_type="validation_retry", call_kind="text", model=llm_model,
                    attempt=attempt + 1, retry_reason="validation",
                    validation_retries=attempt + 1, status="ok",
                )
        if last_valid:
            _mark_quality_applies(quality, last_valid)
            return last_valid, [], "", quality
        _mark_quality_applies(quality, "")
        return "", violations or ["改写结果为空"], last_output, quality

    # ─── 条目构造 ───

    @staticmethod
    def _make_item(segment: Dict[str, Any], rewritten_prompt: str, mode: str,
                   grounding_meta: Optional[Dict] = None,
                   prev_item: Optional[Dict[str, Any]] = None,
                   fingerprint: str = "",
                   dimensions: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
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
        # Q1：当次输入指纹（空串 = unknown，不得视为新鲜）
        item["input_fingerprint"] = fingerprint
        item["input_dimensions"] = dict(dimensions or {})
        if segment.get("duration_adjustments"):
            # Q2：时长夹取事实随条目透出（与视频阶段同源）
            item["duration_adjustment"] = list(segment["duration_adjustments"])
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
                                continuity_seeds: Optional[Dict[str, str]] = None,
                                ordered_segments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
        """逐分镜串行改写（continuity 链）；条目级失败隔离，实时透出条目完成事件。

        `segments` 是本次要执行的条目；`ordered_segments` 是完整有序分镜，用于
        计算首/尾帧邻接（B3）。未提供时两者相同（全量执行）。传入**空列表**会被当作
        “无邻接”（不要用空列表表达“不限制”）。
        """
        session_meta = self._session_meta(input_data)
        llm_model = (
            str(Config.H3_REWRITE_LLM_MODEL or "").strip()
            or str(input_data.get("llm_model") or session_meta.get("llm_model") or "").strip()
            or str(Config.LLM_MODEL or "").strip()
        )
        vlm_model = resolve_grounding_model(input_data, session_meta)
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
        template_version = content_version(load_prompt("prompt_rewrite", "rewrite", "zh"))
        ordered = ordered_segments if ordered_segments is not None else segments

        # continuity 链：串行累积各分镜的主体/风格摘要
        continuity_summaries: List[str] = list(continuity_seeds or [])
        results: Dict[str, Dict[str, Any]] = {}
        total = len(segments)
        grounding_degraded = 0  # M-d：text_only 降级条目计数，阶段完成时汇总可见
        for i, segment in enumerate(segments):
            self._check_cancel()
            prev_item = prev_items_by_id.get(segment["id"])
            # mode resolver：素材角色来自参考图阶段产物（start_end_frame 的尾帧 = 下一分镜选中图，
            # 与 video_agent 的首尾帧取图逻辑一致；尾帧缺失时 resolver 落 I2VA，与传输层回退一致）
            selected = str((scene_map.get(segment["id"]) or {}).get("selected") or "").strip()
            next_selected = ""
            if video_generation_mode == "start_end_frame":
                nxt = self._next_in_order(ordered, segment["id"])
                if nxt is not None:
                    next_selected = str((scene_map.get(nxt["id"]) or {}).get("selected") or "").strip()
            mode = resolve_h3_mode(
                video_generation_mode,
                has_first_frame=bool(selected),
                has_last_frame=bool(next_selected),
                has_reference_set=(video_generation_mode == "reference"),
            )
            guide = await asyncio.to_thread(self._load_guide, mode)
            # 参考图清单：grounding 与 <Picture N> 数量校验共用同一取法（含 L2VA 尾帧）。
            reference_images = self._grounding_images(
                segment, ordered, scene_map, mode, artifacts=artifacts,
            )
            picture_count = len(reference_images)
            # C：VLM grounding（独立开关；失败回退文本输入，模式不变）
            if mode in ("I2VA", "FL2VA", "Ref2VA"):
                grounding_desc, grounding_hashes = await asyncio.to_thread(
                    self._grounding_describe,
                    reference_images,
                    vlm_model,
                    str(input_data.get("session_id") or session_meta.get("session_id") or ""),
                )
            else:
                grounding_desc, grounding_hashes = "", []
            if grounding_desc or grounding_hashes:
                grounding_meta = {"image_hash": grounding_hashes[0] if grounding_hashes else "",
                                  "image_hashes": list(grounding_hashes),
                                  "model": vlm_model,
                                  "described": bool(grounding_desc)}
            elif mode in ("I2VA", "FL2VA", "Ref2VA"):
                grounding_meta = {"described": False, "text_only": True, "model": vlm_model}
                grounding_degraded += 1
            else:
                grounding_meta = None

            # Q1：当次输入指纹（grounding 只纳入模型标识，文本是其派生结果）
            fingerprint, dimensions = self._input_fingerprint_parts(
                segment, mode, ordered, scene_map, artifacts, input_data, session_meta,
                grounding_model=vlm_model,
            )

            sections = self._render_sections(
                authoritative=self._authoritative_context(segment, character_artifact),
                shot_content=segment["shot_content"],
                user_section=(user_sections or {}).get(segment["id"], ""),
                continuity_summaries=continuity_summaries,
                grounding_desc=grounding_desc,
            )

            item = self._make_item(segment, "", mode, grounding_meta, prev_item, fingerprint=fingerprint, dimensions=dimensions)
            # 条目级「改写中」事件：进入 _rewrite_one（含 1+2 次调用的重试环）之前发一次，
            # 沿用 _event_item_summary 但剔除 rewritten_prompt / truncated（D1/D2：只带身份与渲染
            # 字段、不带正文）；status=running 不在 wrapped_progress_callback 的落盘集合内，天然不写盘。
            # 串行执行模型下每条恰好 running→终态 两条；重试在 _rewrite_one 内部，不在此处再发。
            running_summary = {
                key: value
                for key, value in _event_item_summary(item).items()
                if key not in ("rewritten_prompt", "truncated")
            }
            self._report_progress(
                "提示词改写",
                f"分镜 {segment['id']} 改写中",
                round(i / total * 100),
                data={
                    "asset_complete": {
                        "type": "items",
                        "id": segment['id'],
                        "status": "running",
                        **running_summary,
                    }
                },
            )
            quality: Optional[Dict[str, Any]] = None
            try:
                with call_context(
                    operation="prompt_rewrite", template_name="rewrite_zh",
                    template_version=template_version,
                ):
                    rewritten, violations, rejected, quality = await self._rewrite_one(
                        segment, mode, system_prompt, guide, llm_model, sections,
                        picture_count=picture_count,
                    )
                if rewritten:
                    item = self._make_item(segment, rewritten, mode, grounding_meta, prev_item, fingerprint=fingerprint, dimensions=dimensions)
                    self._append_version(item, rewritten, source="agent")
                    item["continuity"] = _continuity_from_prompt(rewritten, mode)
                    continuity_summaries.append(item["continuity"])
                else:
                    item = self._make_item(segment, "", mode, grounding_meta, prev_item, fingerprint=fingerprint, dimensions=dimensions)
                    item["status"] = "failed"
                    item["error"] = "结构校验未通过：" + "；".join(violations)
                    if rejected:
                        # 保留最后被拒输出供诊断/人工采用；超长时截断并显式标记
                        item["rejected_output"] = rejected[:REJECTED_OUTPUT_LIMIT]
                        item["rejected_output_truncated"] = len(rejected) > REJECTED_OUTPUT_LIMIT
            except Exception as exc:  # noqa: BLE001 - 单条目失败不阻塞其他条目
                logger.warning("prompt_rewrite: 分镜 %s 改写失败: %s", segment["id"], exc)
                item = self._make_item(segment, "", mode, grounding_meta, prev_item, fingerprint=fingerprint, dimensions=dimensions)
                item["status"] = "failed"
                item["error"] = str(exc)
            if quality is not None:
                item["quality"] = quality
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
                        **_event_item_summary(item),
                    }
                },
            )
        if grounding_degraded:
            logger.info(
                "prompt_rewrite: grounding 降级 %d/%d 条目（text_only，H3 模式不变；检查 VLM 配置或看图服务）",
                grounding_degraded,
                total,
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

        # 时长与视频阶段同源（Q2）：会话覆盖 > 分镜 > 默认，再按所选模型能力夹取
        session_meta = self._session_meta(input_data)
        _, video_model = VideoDirectorAgent._select_video_model(input_data, session_meta)
        session_override = input_data.get("video_duration") or session_meta.get("video_duration")
        segments = self._collect_segments(
            storyboard_artifact, model=video_model, session_override=session_override
        )
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
            # 单条重生成不携带 continuity 链（独立重写），但保留权威事实与 grounding；
            # 邻接仍按完整列表计算（B3）：目标集合只决定执行哪些条目
            results = await self._rewrite_segments(
                target_segments, input_data, prev_items_by_id, ordered_segments=segments
            )
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
            targets, input_data, prev_items_by_id, user_sections=user_sections,
            ordered_segments=segments,
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
