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
    r"^\s*(?:(?:剧本名称|版本|版权|页眉|页码|第\s*\d+\s*页(?:\s*共\s*\d+\s*页)?)\s*[:：]?|(?:标题|作者)\s*[:：]|(?:Title|Duration|Genre|Style)\s*[:：])",
    re.I,
)
_SUBTITLE_RE = re.compile(r"^\s*【\s*字幕叠加\s*】\s*(.*)$")
_END_CARD_RE = re.compile(r"^\s*【\s*定帧\s*】\s*(.*)$")
_PLACEHOLDER_RE = re.compile(r"^\s*[\[【（(]\s*[^\]】）)]{0,12}预留位\s*[\]】）)]")
_SCENE_DESC_RE = re.compile(r"^\s*【\s*画面描述\s*】")
# 「[场景：李大海家中，客厅]」这类行内场景志不是动作/台词
_BRACKET_SCENE_RE = re.compile(r"^\s*[\[【]\s*(?:场景|地点|时间|画面|镜头)\s*[:：]")
_NARRATION_RE = re.compile(
    r"^\s*(?:【\s*旁白\s*[（(]?([^）)】]*)[）)]?\s*】|(?:旁白|Narration|VO|V\.O\.)\s*[:：])",
    re.I,
)
_CAST_HEAD_RE = re.compile(
    r"^\s*(?:(?:角色设定|人物设定|角色介绍|人物介绍|人物表|角色表)\s*[:：]?\s*(.*)"
    r"|(?:人物|角色)\s*[:：]\s*"
    r"|(?:Characters?|Cast)\s*[:：]\s*(.*))$",
    re.I,
)
_CAST_META_HEAD_RE = re.compile(
    r"^\s*(?:角色设定|人物设定|角色介绍|人物介绍|人物表|角色表)", re.I
)
_CAST_ITEM_RE = re.compile(r"^\s*([^\s:：]{1,24})\s*[:：]\s*(.+)$")
# 「李大海 - 45岁，小微企业主」这类破折号条目也常见于上传剧本的人物段
_CAST_ITEM_DASH_RE = re.compile(r"^\s*([^\s:：\-—–]{1,24})\s*[-—–]{1,2}\s*(.+)$")
_DIALOGUE_RE = re.compile(
    r"^([^:：]{1,24}?)(?:[（(]([^）)]{0,40})[）)])?[:：]\s*(.+)$"
)

# 剧本头信息（前言区）：只有存在幕/场/集标记时才启用，避免把无结构剧本的正文当片头
_FRONT_MATTER_LEVELS = (LEVEL_EPISODE, LEVEL_ACT, LEVEL_SCENE)
_BOOK_TITLE_RE = re.compile(r"^《[^》]{1,60}》$")
_WORK_TITLE_TAIL_RE = re.compile(r"(?:短片|短视频|微电影|宣传片|剧本|脚本)\s*$")
_LABEL_TITLE_RE = re.compile(r"^([^：:]{1,10})[：:]\s*(\S.{0,60})$")
_LABEL_TITLE_BLOCKLIST = {
    "人物", "角色", "人物设定", "角色设定", "场景", "画面", "镜头", "地点", "时间",
    "字幕", "字幕叠加", "定帧", "旁白", "音效", "音乐", "转场", "特效", "标题",
    "片名", "主题", "说明", "备注", "剧情", "梗概", "简介", "概述", "大纲",
}
_SYNOPSIS_HEAD_RE = re.compile(
    r"^\s*(?:剧情梗概|故事梗概|内容简介|剧情简介|故事简介|内容概述|剧本梗概"
    r"|剧本大纲|故事大纲|梗概|简介|概述|大纲)\s*[:：]?\s*$"
)


def _cast_item(text: str):
    """角色段条目：优先「名字：描述」，其次「名字 - 描述」。"""
    value = str(text or "").strip()
    match = _CAST_ITEM_RE.match(value)
    if match:
        return match
    return _CAST_ITEM_DASH_RE.match(value)


def _is_structure_heading(text: str) -> bool:
    """集/幕/场/Markdown 标题：角色段遇到它必须结束。"""
    value = str(text or "").strip()
    return bool(
        _EPISODE_RE.match(value)
        or _ACT_RE.match(value)
        or _SCENE_RE.match(value)
        or _MARKDOWN_RE.match(value)
    )


def _front_matter_end(candidates: List[dict]) -> Optional[int]:
    """前言区结束行（第一个集/幕/场标记），没有结构标记时返回 None。"""
    stops = [
        int(item["line"])
        for item in candidates
        if item.get("level") in _FRONT_MATTER_LEVELS
    ]
    return min(stops) if stops else None


def _looks_like_work_title(text: str) -> bool:
    body = _strip_markup(text)
    if not body or len(body) > 30:
        return False
    if _BOOK_TITLE_RE.match(body):
        return True
    if re.search(r"[:：]", body):
        return False
    return bool(_WORK_TITLE_TAIL_RE.search(body))


def _looks_like_label_title(text: str, cast_names: Iterable[str] = ()) -> bool:
    """前言区「短标签：说明」形标题（如「带你了解：……」），角色名不算标题。"""
    match = _LABEL_TITLE_RE.match(_strip_markup(text))
    if not match:
        return False
    label = match.group(1).strip()
    if not label or label in _LABEL_TITLE_BLOCKLIST:
        return False
    return label not in set(cast_names)


def _synopsis_span(
    lines: List[str],
    candidates: List[dict],
    front_end: Optional[int],
) -> Optional[Tuple[int, int]]:
    """剧情梗概/简介段的行区间（仅前言区），整段只做元信息标注。"""
    if front_end is None:
        return None
    limit = min(front_end, len(lines))
    heading_lines = {int(item["line"]) for item in candidates}
    for index in range(limit):
        if not _SYNOPSIS_HEAD_RE.match(_strip_markup(lines[index])):
            continue
        end = index
        for follow in range(index + 1, limit):
            stripped = lines[follow].strip()
            if not stripped:
                continue
            judge = _strip_markup(stripped)
            if (
                follow in heading_lines
                or _CAST_HEAD_RE.match(judge)
                or _META_LINE_RE.match(judge)
                or _DURATION_RE.match(judge)
                or _TONE_RE.match(judge)
            ):
                break
            end = follow
        return index, end
    return None


def _front_matter_annotation(
    stripped: str,
    judge: str,
    index: int,
    synopsis_span: Optional[Tuple[int, int]],
    cast_names: Iterable[str],
) -> Optional[Dict[str, Any]]:
    if synopsis_span and synopsis_span[0] <= index <= synopsis_span[1]:
        return {"type": "meta"}
    if _looks_like_work_title(judge):
        return {"type": "heading", "unit": LEVEL_TITLE, "title": _clean_heading(stripped)}
    if _looks_like_label_title(judge, cast_names):
        return {"type": "heading", "unit": LEVEL_TITLE, "title": _clean_heading(stripped)}
    return None


def _text_annotation(judge: str) -> Dict[str, Any]:
    """非结构化正文行的兜底判定：台词 / 章节标签 / 动作。"""
    dialogue = _DIALOGUE_RE.match(judge)
    if not dialogue:
        return {"type": "action"}
    speaker = dialogue.group(1).strip().strip("*_` ")
    if speaker in {"人物", "场景", "画面", "镜头", "地点", "时间", "字幕叠加", "定帧"}:
        return {"type": "scene_desc"}
    return {
        "type": "dialogue",
        "speaker": speaker,
        "tone": (dialogue.group(2) or "").strip(),
    }


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
    head_match = _CAST_HEAD_RE.match(_strip_markup(lines[start]))
    inline = ((head_match.group(1) if head_match else "") or
              (head_match.group(2) if head_match else "") or "").strip()
    inline_item = _cast_item(inline) if inline else None
    if inline and not inline_item:
        # 行内名单（如 `Characters: Alice, Bob` / `Characters: Alice`）：单行角色段
        return start, start
    end = start
    saw_item = bool(inline_item)
    is_meta_section = bool(_CAST_META_HEAD_RE.match(_strip_markup(lines[start])))
    seen_names: set[str] = set()
    if inline_item:
        for single in [part.strip() for part in inline_item.group(1).split("/") if part.strip()]:
            seen_names.add(single)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped:
            # 首个条目之前允许空行；出现条目后：人物清单遇空行结束，
            # 角色设定/人物表允许空行分隔条目（后一行仍是新条目时继续）。
            if not saw_item:
                continue
            if is_meta_section:
                look = index + 1
                while look < len(lines) and not lines[look].strip():
                    look += 1
                if look < len(lines):
                    nxt = lines[look].strip()
                    nxt_item = _cast_item(nxt)
                    if (nxt_item and not re.match(r"^【", nxt)
                            and not _is_structure_heading(nxt)
                            and nxt_item.group(1).strip() not in seen_names):
                        continue
            break
        if (_NARRATION_RE.match(stripped) or _SCENE_DESC_RE.match(stripped)
                or _BRACKET_SCENE_RE.match(stripped)
                or _SUBTITLE_RE.match(stripped) or _END_CARD_RE.match(stripped)
                or _is_structure_heading(stripped)
                or stripped.lower().startswith("<action>")):
            break
        item = _cast_item(stripped)
        if not item or re.match(r"^【", stripped):
            break
        name = item.group(1).strip()
        if name in seen_names:
            # 重复名字基本可判定为对白，而非角色条目
            break
        seen_names.add(name)
        saw_item = True
        end = index
    return start, end


def extract_cast(lines: List[str]) -> List[dict]:
    """从显式角色设定段解析 name/description，原文优先。"""
    start, end = _cast_section(lines)
    if start is None or end is None:
        return []
    characters: List[dict] = []
    head_match = _CAST_HEAD_RE.match(_strip_markup(lines[start]))
    inline = ((head_match.group(1) if head_match else "") or
              (head_match.group(2) if head_match else "") or "").strip()
    if not inline and end <= start:
        return []
    if inline:
        # 「角色设定：小陈：…」形式，补一行虚拟解析
        item = _cast_item(inline)
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
        elif "," in inline or "、" in inline:
            # 英文/中文名单：`Characters: Alice, Bob` / `角色设定：小明、小红`（无描述）
            for single in [part.strip() for part in re.split(r"[,，、]", inline) if part.strip()]:
                characters.append(
                    {"name": single, "description": "", "description_source": "uploaded"}
                )
        else:
            # 单个英文名：`Characters: Alice`
            characters.append(
                {"name": inline, "description": "", "description_source": "uploaded"}
            )
    seen = {item["name"] for item in characters}
    for line in lines[start + 1:end + 1]:
        stripped = line.strip()
        item = _cast_item(stripped)
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
    # 剧本头（片名/副标题/梗概）只在“后面确实存在幕/场/集标记”时识别，
    # 否则无结构剧本的开头对白会被误判成片头标题。
    front_end = _front_matter_end(candidates)
    cast_names: set = set()
    synopsis_span: Optional[Tuple[int, int]] = None
    if front_end is not None:
        cast_names = {
            str(entry.get("name") or "").strip()
            for entry in extract_cast(lines)
            if str(entry.get("name") or "").strip()
        }
        synopsis_span = _synopsis_span(lines, candidates, front_end)
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
        elif _SCENE_DESC_RE.match(judge) or _BRACKET_SCENE_RE.match(judge):
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
            front_item = None
            if front_end is not None and index < front_end:
                front_item = _front_matter_annotation(
                    stripped, judge, index, synopsis_span, cast_names
                )
            item.update(front_item or _text_annotation(judge))
        annotations.append(item)
    return annotations


# ─────────────── 场景映射 ───────────────

def deterministic_annotations(lines: List[str]) -> List[dict]:
    """导入路径的确定性标注：候选标题 + 角色设定区间，与导入入口同口径。

    正文手改后重建坐标时必须复用它，否则角色设定段会被当成对白（R15）。
    """
    return annotate_lines(lines, detect_candidates(lines), _cast_section(lines))


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
        record = {
            "character_id": item.get("character_id") or stable_id("char", name),
            "name": name,
            "description": item.get("description", ""),
            "species": item.get("species") or extra.get("species", ""),
            "description_source": "uploaded",
        }
        kind = item.get("asset_kind") or extra.get("asset_kind") or ""
        if kind:
            record["asset_kind"] = kind
        merged.append(record)
        used.add(name)
    for name, item in inferred_by_name.items():
        if name in used:
            continue
        record = {
            "character_id": item.get("character_id") or stable_id("char", name),
            "name": name,
            "description": item.get("description", ""),
            "species": item.get("species", ""),
            "description_source": "inferred",
        }
        if item.get("asset_kind"):
            record["asset_kind"] = item["asset_kind"]
        merged.append(record)
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
        record = {
            "setting_id": item.get("setting_id") or stable_id("set", name),
            "name": name,
            "description": item.get("description", ""),
            "description_source": "inferred",
            "aliases": list(item.get("aliases") or []),
        }
        if item.get("asset_kind"):
            record["asset_kind"] = item["asset_kind"]
        merged.append(record)
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


# ─────────────── 导入质检（历史结果规范化：新流程不再触发，改走剧本 AI 质检） ───────────────

MIN_UNIT_DURATION = 2
MAX_UNIT_DURATION = 15
# 画面描述行不单独成镜头，但它的停留建议会合并到同场景的下一个动作/台词
_DURATION_TYPES = {"action", "dialogue", "narration", "scene_desc", "placeholder"}
_MAX_REVIEW_ISSUES = 20
_MAX_REVIEW_DURATIONS = 200
_MAX_REVIEW_SECONDS = 7200
_REVIEW_ISSUE_TYPES = {"content", "order", "timing", "pacing"}
_REVIEW_SEVERITIES = {"high", "medium", "low"}


def _positive_int(value: Any) -> Optional[int]:
    """只接受正整数；0、负数、非数字及非有限值（Infinity/NaN）一律视为无效建议。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _review_text(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def apply_import_review(
    review: Any,
    annotations: List[dict],
) -> Tuple[Optional[Dict[str, Any]], List[dict]]:
    """规范化导入质检结果，并把建议时长合并到行标注（历史工具）。

    导入阶段已不再触发质检；本函数保留用于旧产物兼容、离线分析或迁移工具。

    - 只接受原文中真实存在的行号，虚构行号一律丢弃；
    - 建议时长夹取到 2-15 秒，且只落到动作/台词/旁白行；
    - 不产生正文改写，因此不破坏保真导入契约。
    """
    if not isinstance(review, dict):
        return None, list(annotations or [])

    line_types: Dict[int, str] = {}
    for item in annotations or []:
        if not isinstance(item, dict):
            continue
        try:
            line_no = int(item.get("line"))
        except (TypeError, ValueError):
            continue
        line_types[line_no] = str(item.get("type") or "")

    issues: List[dict] = []
    raw_issues = review.get("issues")
    if isinstance(raw_issues, list):
        for item in raw_issues:
            if not isinstance(item, dict) or len(issues) >= _MAX_REVIEW_ISSUES:
                continue
            try:
                line_no = int(item.get("line"))
            except (TypeError, ValueError):
                continue
            if line_no not in line_types:
                continue
            problem = _review_text(item.get("problem"))
            if not problem:
                continue
            issue_type = str(item.get("type") or "").strip().lower()
            severity = str(item.get("severity") or "").strip().lower()
            issues.append(
                {
                    "line": line_no,
                    "type": issue_type if issue_type in _REVIEW_ISSUE_TYPES else "pacing",
                    "severity": severity if severity in _REVIEW_SEVERITIES else "medium",
                    "problem": problem,
                    "suggestion": _review_text(item.get("suggestion")),
                }
            )

    duration_by_line: Dict[int, int] = {}
    raw_durations = review.get("unit_durations")
    if isinstance(raw_durations, list):
        for item in raw_durations:
            if not isinstance(item, dict) or len(duration_by_line) >= _MAX_REVIEW_DURATIONS:
                continue
            try:
                line_no = int(item.get("line"))
            except (TypeError, ValueError):
                continue
            if line_types.get(line_no) not in _DURATION_TYPES:
                continue
            seconds = _positive_int(item.get("seconds"))
            if seconds is not None:
                duration_by_line[line_no] = max(
                    MIN_UNIT_DURATION, min(MAX_UNIT_DURATION, seconds)
                )

    suggested_total = _positive_int(review.get("suggested_total_seconds"))
    if suggested_total is not None:
        suggested_total = max(MIN_UNIT_DURATION, min(_MAX_REVIEW_SECONDS, suggested_total))

    normalized = {
        "summary": _review_text(review.get("summary")),
        "issues": issues,
        "unit_durations": [
            {"line": line_no, "seconds": seconds}
            for line_no, seconds in sorted(duration_by_line.items())
        ],
        "suggested_total_seconds": suggested_total,
        "duration_reason": _review_text(review.get("duration_reason")),
        "source": "llm",
    }

    if not any(
        (
            normalized["summary"],
            normalized["issues"],
            normalized["unit_durations"],
            normalized["suggested_total_seconds"],
        )
    ):
        return None, list(annotations or [])

    updated: List[dict] = []
    for item in annotations or []:
        if isinstance(item, dict):
            try:
                line_no = int(item.get("line"))
            except (TypeError, ValueError):
                line_no = -1
            seconds = duration_by_line.get(line_no)
            if seconds is not None:
                updated.append({**item, "duration": seconds})
                continue
        updated.append(item)
    return normalized, updated


def mark_import_review_stale(artifact: Any) -> None:
    """正文变更后让质检结论与建议总时长同时失效（手工编辑与 AI 修订共用）。"""
    if not isinstance(artifact, dict):
        return
    review = artifact.get("import_review")
    if isinstance(review, dict):
        review["stale"] = True
    meta = artifact.get("meta")
    if isinstance(meta, dict):
        meta.pop("suggested_total_seconds", None)
