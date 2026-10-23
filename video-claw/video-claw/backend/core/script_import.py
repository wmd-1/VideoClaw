# -*- coding: utf-8 -*-
"""上传剧本的保真解析器。

约束（openspec: script-import-fidelity）：
- 正文只做按行切片，不改写、不裁剪、不补写；
- 切片必须能还原抽取基线，失败即回退整篇 1 集；
- 只有"集"级标记参与切集，"幕/场"只做标注与场景映射；
- 角色设定段优先于模型提取，原文描述标 uploaded。

模块不依赖 agent/orchestrator，所有 LLM 结果通过参数传入。
"""

import hashlib
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 候选标记级别
LEVEL_EPISODE = "episode"
LEVEL_ACT = "act"
LEVEL_SCENE = "scene"
LEVEL_TITLE = "title"

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100}


def _cn_to_int(text: str) -> Optional[int]:
    """把阿拉伯数字或简单中文数字转为 int；无法识别返回 None。"""
    text = str(text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = 0
    current = 0
    for char in text:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            total += (current or 1) * unit
            current = 0
        else:
            return None
    total += current
    return total or None


def normalize_text(raw: str) -> str:
    """去 BOM/零宽字符，统一换行；不改变行内其他字符。"""
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\ufeff", "")
    text = re.sub(r"[\u200b\u200c\u200d\u2060]", "", text)
    return text


def split_lines(raw: str) -> List[str]:
    return normalize_text(raw).split("\n")


# ─────────────── L1：确定性候选识别 ───────────────

_EPISODE_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:第\s*([0-9一二三四五六七八九十百两]+)\s*集(?![-－])|Episode\s+(\d+)\b)",
    re.IGNORECASE,
)
_ACT_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:第\s*([0-9一二三四五六七八九十百两]+)\s*幕|Act\s+(\d+)\b|【\s*第\s*([0-9一二三四五六七八九十百两]+)\s*幕)",
    re.IGNORECASE,
)
_SCENE_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*|__)?\s*(?:第\s*([0-9一二三四五六七八九十百两]+)\s*场|Scene\s+(\d+)\b)",
    re.IGNORECASE,
)
_MARKDOWN_RE = re.compile(r"^\s*#{1,6}\s+\S")


def _clean_heading(text: str) -> str:
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned)
    cleaned = cleaned.strip("*_` \t")
    cleaned = cleaned.strip("【】[]")
    return cleaned.strip()


def _strip_markup(text: str) -> str:
    """仅用于类型判断：去行首 #/> 与两端 * _ `；不改原文。"""
    cleaned = re.sub(r"^[#\s>]+", "", str(text or "").strip())
    return cleaned.strip("*_` \t").strip()


def detect_candidates(lines: List[str]) -> List[dict]:
    """识别集/幕/场/Markdown 标题候选行。"""
    candidates: List[dict] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        match = _EPISODE_RE.match(stripped)
        if match:
            number = _cn_to_int(match.group(1) or match.group(2) or "")
            candidates.append(
                {
                    "line": index,
                    "level": LEVEL_EPISODE,
                    "number": number,
                    "title": _clean_heading(stripped),
                }
            )
            continue
        match = _ACT_RE.match(stripped)
        if match:
            number = _cn_to_int(match.group(1) or match.group(2) or match.group(3) or "")
            candidates.append(
                {
                    "line": index,
                    "level": LEVEL_ACT,
                    "number": number,
                    "title": _clean_heading(stripped),
                }
            )
            continue
        match = _SCENE_RE.match(stripped)
        if match:
            number = _cn_to_int(match.group(1) or match.group(2) or "")
            candidates.append(
                {
                    "line": index,
                    "level": LEVEL_SCENE,
                    "number": number,
                    "title": _clean_heading(stripped),
                }
            )
            continue
        if _MARKDOWN_RE.match(stripped):
            candidates.append(
                {
                    "line": index,
                    "level": LEVEL_TITLE,
                    "number": None,
                    "title": _clean_heading(stripped),
                }
            )
    return candidates


def needs_llm_decision(candidates: List[dict]) -> bool:
    """只有"多级候选混杂且无明确集标记"时才需要模型裁决。"""
    if not candidates:
        return False
    levels = {item["level"] for item in candidates}
    if LEVEL_EPISODE in levels:
        return False
    # 只有幕/场：不切集，无需裁决
    if levels <= {LEVEL_ACT, LEVEL_SCENE}:
        return False
    # 多个 Markdown 标题但无集/幕/场：可能是多集，也可能只是章节，交给模型
    return LEVEL_TITLE in levels and len([c for c in candidates if c["level"] == LEVEL_TITLE]) >= 2


def _is_episode_like(candidate: dict) -> bool:
    """只有集级候选或标题本身形如集时才允许作为切分点。"""
    if candidate.get("level") == LEVEL_EPISODE:
        return True
    if candidate.get("level") != LEVEL_TITLE:
        return False
    title = str(candidate.get("title") or "")
    return bool(
        re.search(
            r"(?:第\s*[0-9一二三四五六七八九十百两]+\s*集|Episode\s+\d+)",
            title,
            re.IGNORECASE,
        )
    )


def decide_episode_starts(
    candidates: List[dict],
    llm_lines: Optional[Iterable[int]] = None,
) -> Tuple[List[int], str]:
    """返回集级起点行号与裁决来源（rule/llm/fallback）。"""
    episode_lines = sorted(
        {c["line"] for c in candidates if c["level"] == LEVEL_EPISODE}
    )
    by_line = {item["line"]: item for item in candidates}
    chosen: List[int] = []
    if llm_lines is not None:
        chosen = sorted(
            {
                int(value)
                for value in llm_lines
                if str(value).lstrip("-").isdigit()
                and int(value) in by_line
                and _is_episode_like(by_line[int(value)])
            }
        )
    if episode_lines:
        # 存在集级候选时必须全取；模型只给部分集号会导致静默并集
        if chosen and set(chosen) == set(episode_lines):
            return chosen, "llm"
        return episode_lines, "rule"
    if chosen:
        return chosen, "llm"
    return [], "fallback"


def source_format(candidates: List[dict], starts: List[int]) -> str:
    if starts:
        return "episode"
    levels = {c["level"] for c in candidates}
    if LEVEL_ACT in levels:
        return "act"
    if LEVEL_SCENE in levels:
        return "scene"
    if LEVEL_TITLE in levels:
        return "markdown"
    return "plain"


# ─────────────── L3：切片与还原校验 ───────────────

def _episode_title(lines: List[str], start: int, number: int) -> str:
    title = _clean_heading(lines[start]) if 0 <= start < len(lines) else ""
    if title:
        return title
    return f"第{number}集"


def split_episodes(lines: List[str], starts: List[int]) -> List[dict]:
    """按集级起点切片；第一个起点之前的前言并入首集，不单独成集。"""
    if not starts:
        return [
            {
                "episode_number": 1,
                "act_title": "第1集",
                "content": "\n".join(lines),
                "line_start": 0,
                "line_end": max(0, len(lines) - 1),
            }
        ]
    raw_boundaries = sorted({max(0, min(int(s), len(lines) - 1)) for s in starts})
    boundaries = list(raw_boundaries)
    if boundaries and boundaries[0] > 0:
        boundaries[0] = 0
    episodes: List[dict] = []
    for order, start in enumerate(boundaries):
        end = (boundaries[order + 1] - 1) if order + 1 < len(boundaries) else len(lines) - 1
        number = order + 1
        # 前言并入首集时，标题仍取第一个集标记行，而不是片名
        title_line = (
            raw_boundaries[0]
            if order == 0 and raw_boundaries and raw_boundaries[0] > 0
            else start
        )
        episodes.append(
            {
                "episode_number": number,
                "act_title": _episode_title(lines, title_line, number),
                "content": "\n".join(lines[start:end + 1]),
                "line_start": start,
                "line_end": end,
            }
        )
    return episodes


def validate_slices(lines: List[str], episodes: List[dict]) -> None:
    """所有切片按序拼接必须与基线逐字一致，否则抛 ValueError。"""
    if not episodes:
        raise ValueError("切片结果为空")
    rebuilt: List[str] = []
    expected_start = 0
    for episode in episodes:
        start = int(episode.get("line_start", -1))
        end = int(episode.get("line_end", -1))
        if start < 0 or end < start or end >= len(lines):
            raise ValueError(f"切片行区间非法：{start}-{end}")
        if start != expected_start:
            raise ValueError(f"切片不连续：期望起始 {expected_start}，实际 {start}")
        content_lines = str(episode.get("content", "")).split("\n")
        if content_lines != lines[start:end + 1]:
            raise ValueError(f"第 {episode.get('episode_number')} 集正文与基线不一致")
        rebuilt.extend(content_lines)
        expected_start = end + 1
    if rebuilt != lines:
        raise ValueError("切片拼接结果与基线不一致")


# ─────────────── 元信息与行级标注 ───────────────

_DURATION_RE = re.compile(r"^\s*(?:视频)?时长\s*[:：]\s*(.+)$")
_TONE_RE = re.compile(r"^\s*(?:视频)?基调\s*[:：]\s*(.+)$")
_META_LINE_RE = re.compile(
    r"^\s*(?:(?:剧本名称|版本|版权|页眉|页码|第\s*\d+\s*页(?:\s*共\s*\d+\s*页)?)\s*[:：]?|(?:标题|作者)\s*[:：])"
)
_SUBTITLE_RE = re.compile(r"^\s*【\s*字幕叠加\s*】\s*(.*)$")
_END_CARD_RE = re.compile(r"^\s*【\s*定帧\s*】\s*(.*)$")
_PLACEHOLDER_RE = re.compile(r"^\s*[\[【（(]\s*[^\]】）)]{0,12}预留位\s*[\]】）)]")
_SCENE_DESC_RE = re.compile(r"^\s*【\s*画面描述\s*】")
_NARRATION_RE = re.compile(r"^\s*【\s*旁白\s*[（(]?([^）)】]*)[）)]?\s*】")
_CAST_HEAD_RE = re.compile(
    r"^\s*(?:(?:角色设定|人物表|角色表)\s*[:：]?\s*(.*)|(?:人物|角色)\s*[:：]\s*)$"
)
_CAST_ITEM_RE = re.compile(r"^\s*([^\s:：]{1,24})\s*[:：]\s*(.+)$")
_DIALOGUE_RE = re.compile(
    r"^([^:：]{1,24}?)(?:[（(]([^）)]{0,40})[）)])?[:：]\s*(.+)$"
)


def parse_duration_seconds(text: str) -> Optional[int]:
    """支持 90秒 / 约90秒 / 1分30秒 / 1:30 / 90s。"""
    value = str(text or "")
    match = re.search(r"(\d+)\s*分\s*(\d+)\s*秒", value)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    match = re.search(r"(\d+)\s*[:：]\s*(\d+)", value)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    match = re.search(r"(\d+)\s*(?:秒|s|S)\b", value) or re.search(r"(\d+)\s*秒", value)
    if match:
        return int(match.group(1))
    return None


def extract_meta(lines: List[str]) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "target_duration_seconds": None,
        "tone": "",
        "explicit_cast": False,
        "subtitle_overlays": [],
        "end_card": "",
        "placeholders": [],
    }
    normalized = [_strip_markup(line) for line in lines]
    for index, judge in enumerate(normalized):
        if not judge:
            continue
        match = _DURATION_RE.match(judge)
        if match and meta["target_duration_seconds"] is None:
            seconds = parse_duration_seconds(match.group(1))
            if seconds:
                meta["target_duration_seconds"] = seconds
            continue
        match = _TONE_RE.match(judge)
        if match and not meta["tone"]:
            meta["tone"] = match.group(1).strip()
            continue
        match = _SUBTITLE_RE.match(judge)
        if match and match.group(1).strip():
            text = match.group(1).strip().strip("“”\"'")
            if text:
                meta["subtitle_overlays"].append(text)
            continue
        if _PLACEHOLDER_RE.match(judge):
            meta["placeholders"].append({"line": index, "text": judge[:80]})
            continue
        match = _END_CARD_RE.match(judge)
        if match:
            inline = match.group(1).strip()
            if inline:
                meta["end_card"] = inline
            else:
                # `【定帧】` 独占一行时，收集后续文案直到下一个段落标记
                collected: List[str] = []
                for follow in normalized[index + 1:]:
                    if not follow:
                        continue
                    if follow.startswith("【") or _PLACEHOLDER_RE.match(follow):
                        break
                    collected.append(follow)
                    if len(collected) >= 5:
                        break
                if collected:
                    meta["end_card"] = " ".join(collected)
    return meta


def _cast_section(lines: List[str]) -> Tuple[Optional[int], Optional[int]]:
    """返回显式角色设定段的起止行号（含端点）；无则 (None, None)。"""
    start = None
    for index, line in enumerate(lines):
        # 允许 `## 人物表` / `**角色设定：**` 等 Markdown 包装
        if _CAST_HEAD_RE.match(_strip_markup(line)):
            start = index
            break
    if start is None:
        return None, None
    end = start
    saw_item = False
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped:
            # 空行：段内允许一个空行，连续第二个空行视为结束
            if saw_item and index + 1 < len(lines) and not lines[index + 1].strip():
                break
            continue
        if _CAST_ITEM_RE.match(stripped) and not re.match(r"^【", stripped):
            saw_item = True
            end = index
            continue
        break
    return start, end


def extract_cast(lines: List[str]) -> List[dict]:
    """从显式角色设定段解析 name/description，原文优先。"""
    start, end = _cast_section(lines)
    if start is None or end is None or end <= start:
        return []
    characters: List[dict] = []
    head_match = _CAST_HEAD_RE.match(_strip_markup(lines[start]))
    inline = (head_match.group(1) or "").strip() if head_match else ""
    if inline:
        # 「角色设定：小陈：…」形式，补一行虚拟解析
        item = _CAST_ITEM_RE.match(inline)
        if item:
            name = item.group(1).strip()
            for single in [part.strip() for part in name.split("/") if part.strip()] or [name]:
                characters.append(
                    {
                        "name": single,
                        "description": item.group(2).strip(),
                        "description_source": "uploaded",
                    }
                )
    seen = {item["name"] for item in characters}
    for line in lines[start + 1:end + 1]:
        stripped = line.strip()
        item = _CAST_ITEM_RE.match(stripped)
        if not item:
            continue
        name = item.group(1).strip()
        description = item.group(2).strip()
        if not name:
            continue
        # 「浙小控/浙宝箱」合写：拆成独立条目，共享描述
        for single in [part.strip() for part in name.split("/") if part.strip()] or [name]:
            if single in seen:
                continue
            characters.append(
                {
                    "name": single,
                    "description": description,
                    "description_source": "uploaded",
                }
            )
            seen.add(single)
    return characters


def annotate_lines(
    lines: List[str],
    candidates: List[dict],
    cast_range: Tuple[Optional[int], Optional[int]] = (None, None),
) -> List[dict]:
    """为每个非空行贴一个主类型；不复制行内容。"""
    candidate_by_line = {item["line"]: item for item in candidates}
    cast_start, cast_end = cast_range
    annotations: List[dict] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        judge = _strip_markup(stripped)
        item: Dict[str, Any] = {"line": index}
        candidate = candidate_by_line.get(index)
        # 已知段落标记优先于通用标题候选（如 `## 【定帧】`）
        if _PLACEHOLDER_RE.match(judge):
            item.update({"type": "placeholder", "kind": "screen_recording"})
        elif _SCENE_DESC_RE.match(judge):
            item["type"] = "scene_desc"
        elif _NARRATION_RE.match(judge):
            speaker = (_NARRATION_RE.match(judge).group(1) or "").strip()
            item.update({"type": "narration", "speaker": speaker})
        elif _SUBTITLE_RE.match(judge):
            item["type"] = "subtitle"
        elif _END_CARD_RE.match(judge):
            item["type"] = "end_card"
            if candidate:
                item["title"] = candidate.get("title", "")
        elif candidate:
            item.update(
                {
                    "type": "heading",
                    "unit": candidate["level"],
                    "title": candidate["title"],
                }
            )
        elif cast_start is not None and cast_end is not None and cast_start <= index <= cast_end:
            item["type"] = "cast_block"
        elif _META_LINE_RE.match(judge) or _DURATION_RE.match(judge) or _TONE_RE.match(judge):
            item["type"] = "meta"
        elif judge.lower().startswith("<action>"):
            item["type"] = "action"
        else:
            dialogue = _DIALOGUE_RE.match(judge)
            if dialogue:
                speaker = dialogue.group(1).strip().strip("*_` ")
                # 章节标签不是台词
                if speaker in {"人物", "场景", "画面", "镜头", "地点", "时间", "字幕叠加", "定帧"}:
                    item["type"] = "scene_desc"
                else:
                    item.update(
                        {
                            "type": "dialogue",
                            "speaker": speaker,
                            "tone": (dialogue.group(2) or "").strip(),
                        }
                    )
            else:
                item["type"] = "action"
        annotations.append(item)
    return annotations


# ─────────────── 场景映射 ───────────────

def build_scene_spans(
    lines: List[str],
    candidates: List[dict],
    setting_by_heading_line: Optional[Dict[int, str]] = None,
) -> List[dict]:
    """把幕/场标题绑定到行区间；地点由外部传入（LLM 抽取）。"""
    mapping = setting_by_heading_line or {}
    spans: List[dict] = []
    headings = [c for c in candidates if c["level"] in (LEVEL_ACT, LEVEL_SCENE)]
    if not headings:
        return spans
    for order, heading in enumerate(headings):
        end = (
            headings[order + 1]["line"] - 1
            if order + 1 < len(headings)
            else max(heading["line"], len(lines) - 1)
        )
        setting = mapping.get(heading["line"], "")
        if not setting:
            continue
        spans.append(
            {
                "setting": setting,
                "from_line": heading["line"],
                "to_line": end,
                "heading": heading["title"],
            }
        )
    return spans


# ─────────────── 组装 ───────────────

def stable_id(prefix: str, name: str) -> str:
    digest = hashlib.sha1(str(name or "").encode("utf-8")).hexdigest()[:6]
    return f"{prefix}_{digest}"


def merge_characters(
    explicit: List[dict],
    inferred: Optional[List[dict]] = None,
    exclude_names: Optional[Iterable[str]] = None,
) -> List[dict]:
    """显式设定优先；模型只补缺失字段，同名/合写/标题同名不重复。"""
    excluded = {
        str(name).strip() for name in (exclude_names or []) if str(name).strip()
    }

    def _is_excluded(name: str) -> bool:
        text = str(name or "").strip()
        if not text:
            return True
        # 只排除与片名完全相同的条目；片名中包含的产品/机构主体必须保留
        return text in excluded

    inferred_by_name: dict = {}
    for item in inferred or []:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            continue
        raw_name = str(item["name"]).strip()
        if _is_excluded(raw_name):
            continue
        for single in [part.strip() for part in raw_name.split("/") if part.strip()] or [raw_name]:
            if _is_excluded(single) or single in inferred_by_name:
                continue
            inferred_by_name[single] = item
    merged: List[dict] = []
    used = set()
    for item in explicit:
        name = str(item.get("name", "")).strip()
        if _is_excluded(name):
            continue
        extra = inferred_by_name.get(name, {})
        merged.append(
            {
                "character_id": item.get("character_id") or stable_id("char", name),
                "name": name,
                "description": item.get("description", ""),
                "species": item.get("species") or extra.get("species", ""),
                "description_source": "uploaded",
            }
        )
        used.add(name)
    for name, item in inferred_by_name.items():
        if name in used:
            continue
        merged.append(
            {
                "character_id": item.get("character_id") or stable_id("char", name),
                "name": name,
                "description": item.get("description", ""),
                "species": item.get("species", ""),
                "description_source": "inferred",
            }
        )
    return merged


def merge_settings(
    inferred: Optional[List[dict]] = None,
) -> List[dict]:
    """场景以模型抽取为准，按规范名去重，过滤界面类非地点。"""
    blacklist = ("界面", "屏幕", "报表", "网银", "进度条", "弹窗")
    merged: List[dict] = []
    seen = set()
    for item in inferred or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name or any(word in name for word in blacklist):
            continue
        key = re.sub(r"\s+", "", name.lower())
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            {
                "setting_id": item.get("setting_id") or stable_id("set", name),
                "name": name,
                "description": item.get("description", ""),
                "description_source": "inferred",
                "aliases": list(item.get("aliases") or []),
            }
        )
    return merged


def build_import_artifact(
    raw_text: str,
    *,
    session_id: str,
    title: str = "",
    logline: str = "",
    genre: Optional[List[str]] = None,
    mood: str = "",
    film_type: str = "介绍",
    subject_mode: str = "旁白描述",
    llm_start_lines: Optional[Iterable[int]] = None,
    inferred_characters: Optional[List[dict]] = None,
    inferred_settings: Optional[List[dict]] = None,
    setting_by_heading_line: Optional[Dict[int, str]] = None,
) -> Dict[str, Any]:
    """把上传原文解析为与生成路径一致的产物结构（只贴标签，不改正文）。"""
    from datetime import datetime, timezone

    lines = split_lines(raw_text)
    candidates = detect_candidates(lines)
    starts, decided_by = decide_episode_starts(candidates, llm_start_lines)
    episodes = split_episodes(lines, starts)
    try:
        validate_slices(lines, episodes)
    except ValueError:
        episodes = split_episodes(lines, [])
        starts = []
        decided_by = "fallback"
    cast_range = _cast_section(lines)
    explicit_cast = extract_cast(lines)
    meta = extract_meta(lines)
    meta.update(
        {
            "source_format": source_format(candidates, starts if episodes else []),
            "original_text": normalize_text(raw_text),
            "decided_by": decided_by,
            "explicit_cast": bool(explicit_cast),
            "film_type": film_type,
            "subject_mode": subject_mode,
        }
    )
    annotations = annotate_lines(lines, candidates, cast_range)
    spans = build_scene_spans(lines, candidates, setting_by_heading_line)
    characters = merge_characters(
        explicit_cast, inferred_characters, exclude_names=[title]
    )
    settings = merge_settings(inferred_settings)

    return {
        "project_id": f"proj_{session_id}",
        "session_id": session_id,
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "meta": meta,
        "title": title or "上传剧本",
        "logline": logline or "",
        "genre": genre or [],
        "mood": mood or meta.get("tone", ""),
        "characters": characters,
        "settings": settings,
        "episodes": episodes,
        "line_annotations": annotations,
        "scene_spans": spans,
        "source_episode_count": len(episodes),
    }
