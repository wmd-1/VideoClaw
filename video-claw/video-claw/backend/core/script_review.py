# -*- coding: utf-8 -*-
"""剧本 AI 质检（面向剧本正文的可选改写建议）的纯函数集合。

质检只产出「针对某一集某一行的操作建议」，不直接写产物；用户勾选后由
`apply_issue_edits` 一次性应用。操作按行定位，避免模型自由改写整篇正文导致
内容漂移，也让每条建议都可以被单独接受 / 拒绝。
"""

import copy
import re
import unicodedata
from typing import Any, Dict, List, Optional

MAX_REVIEW_ISSUES = 30
MAX_ISSUE_TEXT = 4000
MAX_INSERT_AFTER_PER_LINE = 2
MAX_REPLACE_LINES = 3
MIN_INSERT_GROUNDING_COVERAGE = 0.6
MIN_DIALOGUE_GROUNDING_COVERAGE = 0.2
MAX_DIALOGUE_LENGTH_RATIO = 1.4
MAX_ABS_DIALOGUE_GROWTH = 4
MAX_ACTION_LENGTH_RATIO = 1.4
MAX_ABS_ACTION_GROWTH = 4
MIN_ACTION_GROUNDING_COVERAGE = 0.65

REVIEW_TYPES = {
    "content",
    "structure",
    "description",
    "dialogue",
    "pacing",
    "consistency",
    "format",
}
REVIEW_SEVERITIES = {"high", "medium", "low"}
REVIEW_ACTIONS = {"replace", "insert_after", "delete"}


def episode_lines(content: Any) -> List[str]:
    """按换行切分正文；保留空行与行序，1-based 行号与提示词 / 前端展示一致。"""
    return str(content if content is not None else "").split("\n")


def episode_number(episode: Dict[str, Any], index: int) -> int:
    """集号：优先 episode_number，其次 act_number，最后按顺序号。"""
    raw = episode.get("episode_number", episode.get("act_number", index + 1))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return index + 1


def episode_map(episodes: Any) -> Dict[int, Dict[str, Any]]:
    result: Dict[int, Dict[str, Any]] = {}
    for index, episode in enumerate(episodes or []):
        if isinstance(episode, dict):
            result[episode_number(episode, index)] = episode
    return result


def build_numbered_episodes(episodes: Any) -> str:
    """把每集正文渲染为「集号 + 1-based 行号: 内容」，供质检提示词定位。"""
    blocks: List[str] = []
    for index, episode in enumerate(episodes or []):
        if not isinstance(episode, dict):
            continue
        number = episode_number(episode, index)
        title = str(episode.get("act_title") or "").strip()
        header = f"第{number}集" + (f"《{title}》" if title else "")
        numbered = "\n".join(
            f"{line_no}: {line}"
            for line_no, line in enumerate(episode_lines(episode.get("content")), 1)
        )
        blocks.append(f"{header}\n{numbered}")
    return "\n\n".join(blocks)


def _clean_text(value: Any, limit: int) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:limit]


_WIDTH_TRANSLATION = str.maketrans({
    "。": ".", "、": ",", "，": ",", "．": ".",
    "？": "?", "！": "!", "：": ":", "；": ";",
    "（": "(", "）": ")", "【": "[", "】": "]",
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "—": "-", "–": "-", "·": ".",
})


def _compat_normalized(value: Any) -> str:
    """NFKC + 中文标点宽度归一：用于识别“仅全/半角差异”的伪改写。"""
    text = unicodedata.normalize("NFKC", str(value or ""))
    return text.translate(_WIDTH_TRANSLATION)


# 元信息/制作标记：不参与质检，禁止 replace/delete；空行也一律跳过
_PROTECTED_PREFIXES = (
    "#", "视频时长", "视频基调", "角色设定", "人物设定", "人物：", "人物:",
    "剧情梗概", "故事梗概", "内容简介", "场景：", "场景:",
    "[画面描述]", "【画面描述】", "结尾字幕",
)
_PREFIX_MARKERS = ("字幕", "音效", "特效", "定帧")
_NEW_MARKER_PREFIXES = (
    "【画面描述】", "【场景", "字幕：", "字幕:", "音效：", "音效:",
    "特效：", "特效:", "定帧：", "定帧:",
)
_MARKDOWN_PREFIX_RE = re.compile(r"^[*#>`_\s]+")
_ACTION_TAG_RE = re.compile(r"</?action[^>]*>", re.IGNORECASE)
_DIALOGUE_PREFIX_RE = re.compile(r"^([^：:\n]{1,24}?)(?:[（(][^）)\n]*[）)])?[:：]")
_NON_SPEAKER_LABELS = {
    "人物", "角色", "场景", "画面", "镜头", "地点", "时间",
    "字幕", "音效", "特效", "定帧", "视频时长", "视频基调",
}


def _line_text(line: str) -> str:
    """去 markdown 前缀后的行文本：`**【画面描述】】` 也按制作标记识别。"""
    return _MARKDOWN_PREFIX_RE.sub("", str(line or "").strip())


def _line_kind(line: str) -> str:
    text = _line_text(line)
    for marker in _PREFIX_MARKERS:
        if text.startswith(f"{marker}：") or text.startswith(f"{marker}:"):
            return marker
    if text.startswith("【"):
        return "heading"
    return ""


def _is_protected_line(line: str) -> bool:
    text = str(line or "").strip()
    stripped = _line_text(text)
    if not stripped:
        return True
    return any(
        text.startswith(prefix) or stripped.startswith(prefix)
        for prefix in _PROTECTED_PREFIXES
    )


def _adds_new_marker(original: str, text: str) -> bool:
    """替换/插入不得新增原文没有的制作标记（如 【画面描述】、【场景：、字幕：）。"""
    original_text = _line_text(original)
    for line in str(text or "").split("\n"):
        line_text = _line_text(line)
        if not line_text:
            continue
        for marker in _NEW_MARKER_PREFIXES:
            if line_text.startswith(marker) and not original_text.startswith(marker):
                return True
    return False


def _strip_insert_speaker_prefix(original: str, text: str) -> Optional[str]:
    """剥掉 insert 文本自带的说话人前缀；原行无说话人时也剥。

    续行必须保留说话人前缀：StoryboardAgent._annotate_episode_script 对既非对白
    也非 <action> 的行直接跳过、不生成 unit（不是 2 秒兜底）；因此不能为了少重复
    前缀而剥离它，只能在“接地校验”时临时剥掉再比对正文。

    原行有说话人时，续行换成另一个说话人返回 None；原行无说话人时，insert 自带
    说话人前缀则剥掉后走覆盖率校验，避免“动作行 + 新台词”借宽松路径绕过。
    """
    original_match = _DIALOGUE_PREFIX_RE.match(str(original or "").strip())
    original_speaker = ""
    if original_match:
        candidate = original_match.group(1).strip()
        if candidate and candidate not in _NON_SPEAKER_LABELS:
            original_speaker = candidate
    stripped_lines: List[str] = []
    speakers = set()
    for line in str(text or "").split("\n"):
        line_match = _DIALOGUE_PREFIX_RE.match(line.strip())
        if line_match:
            line_speaker = line_match.group(1).strip()
            if line_speaker and line_speaker not in _NON_SPEAKER_LABELS:
                if original_speaker and line_speaker != original_speaker:
                    return None
                speakers.add(line_speaker)
                stripped_lines.append(line.strip()[line_match.end():])
                continue
        stripped_lines.append(line)
    if not original_speaker and len(speakers) > 1:
        return None
    return "\n".join(stripped_lines)


def _is_grounded_insert(original: str, text: str) -> bool:
    """insert_after 必须有原文依据，防止借说话人前缀夹带凭空台词/动作。

    - 插入文本若自带说话人前缀（含原行无说话人的情况），先剥前缀，再要求正文
      4 字窗口命中率 >=60%；原行有说话人但换成另一个说话人则直接拒绝；
    - 无前缀的画面/动作补充保持“至少一个 4 字窗口命中”，避免误杀镜头停留描述。
    """
    original_norm = re.sub(r"[\s\W_]+", "", str(original or "").lower())
    if not original_norm:
        return False
    body = _strip_insert_speaker_prefix(original, text)
    if body is None:
        return False
    prefixed = body != text
    for line in str(body or "").split("\n"):
        norm = re.sub(r"[\s\W_]+", "", line.lower())
        if not norm:
            continue
        if len(norm) < 4:
            if norm not in original_norm:
                return False
            continue
        windows = [norm[index:index + 4] for index in range(len(norm) - 3)]
        hits = sum(1 for window in windows if window in original_norm)
        if prefixed:
            if hits / len(windows) < MIN_INSERT_GROUNDING_COVERAGE:
                return False
        elif hits == 0:
            return False
    return True


def _keeps_prefix_kind(text: str, kind: str) -> bool:
    lines = [line.strip() for line in str(text or "").split("\n") if line.strip()]
    if not lines:
        return False
    if kind == "heading":
        return all(
            line.startswith("【") or line.startswith("场景：") or line.startswith("场景:")
            for line in lines
        )
    return all(
        line.startswith(f"{kind}：") or line.startswith(f"{kind}:") for line in lines
    )


def _window_coverage(norm: str, reference_norm: str) -> float:
    """4 字窗口命中率；短文本按“是否为子串”二值化。"""
    if not norm:
        return 1.0
    if not reference_norm:
        return 0.0
    if len(norm) < 4:
        return 1.0 if norm in reference_norm else 0.0
    windows = [norm[index:index + 4] for index in range(len(norm) - 3)]
    hits = sum(1 for window in windows if window in reference_norm)
    return hits / len(windows)


def _char_coverage(norm: str, reference_norm: str) -> float:
    """字符覆盖率：对白润色允许换序/增词，用字符重叠而非 4 字窗口。"""
    if not norm:
        return 1.0
    if not reference_norm:
        return 0.0
    reference = set(reference_norm)
    return sum(1 for char in norm if char in reference) / len(norm)


def _is_grounded_replace(original: str, text: str) -> bool:
    """replace 内容接地：对白以原行为参照，动作/描述行要求 4 字窗口 ≥65%。

    - 对白行：剥说话人前缀后，整行字符在原行正文中的覆盖率需 ≥20%；
      允许润色/换序，但凭空新增剧情/政策/承诺会因覆盖过低被拒；
      同时正文总长度不得超过原行正文的 1.4 倍，防止用原行字符重组成长句；
    - 动作/描述行与 <action> 续行：4 字窗口在原行中的命中率需 ≥65%；
      允许“卡片→银行卡”这类小幅润色，拒绝整段新动作/新画面；
    - 原行不足 4 字：允许不超过原行 +3 字的改写，避免短行无法判断。
    """
    match = _DIALOGUE_PREFIX_RE.match(str(original or "").strip())
    speaker = ""
    if match:
        candidate = match.group(1).strip()
        if candidate and candidate not in _NON_SPEAKER_LABELS:
            speaker = candidate
    original_norm = re.sub(r"[\s\W_]+", "", str(original or "").lower())
    if not original_norm:
        return True
    original_body_norm = original_norm
    if speaker and match:
        original_body_norm = re.sub(
            r"[\s\W_]+", "", str(original or "").strip()[match.end():].lower()
        )
    body = _strip_insert_speaker_prefix(original, text)
    if body is None:
        return False
    dialogue_total_len = 0
    action_total_len = 0
    for line in str(body or "").split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        is_action = bool(re.match(r"^</?action", stripped, re.IGNORECASE))
        cleaned = _ACTION_TAG_RE.sub("", stripped)
        norm = re.sub(r"[\s\W_]+", "", cleaned.lower())
        if not norm:
            continue
        if speaker and not is_action:
            # 对白以原行正文为参照，用字符覆盖率允许换序/小幅增词，
            # 避免用本集其他行拼凑出新剧情；同时限制正文长度，防止重组成长句。
            dialogue_total_len += len(norm)
            if _char_coverage(norm, original_body_norm) < MIN_DIALOGUE_GROUNDING_COVERAGE:
                return False
            continue
        action_total_len += len(norm)
        if len(original_norm) < 4:
            if len(norm) > len(original_norm) + 3:
                return False
            continue
        if _window_coverage(norm, original_norm) < MIN_ACTION_GROUNDING_COVERAGE:
            return False
    if dialogue_total_len and len(original_body_norm) > 0:
        original_len = len(original_body_norm)
        max_len = max(
            original_len * MAX_DIALOGUE_LENGTH_RATIO,
            original_len + MAX_ABS_DIALOGUE_GROWTH,
        )
        if dialogue_total_len > max_len:
            return False
    if action_total_len and len(original_norm) > 0:
        original_len = len(original_norm)
        max_len = max(
            original_len * MAX_ACTION_LENGTH_RATIO,
            original_len + MAX_ABS_ACTION_GROWTH,
        )
        if action_total_len > max_len:
            return False
    return True


def _keeps_replace_continuations(original: str, text: str) -> bool:
    """replace 的续行必须能被分镜识别，避免无前缀行被跳过而丢内容。

    - 原文是对白行：首行及每行续行都必须是同一说话人（或 <action>）；
    - 原文是动作/描述行：续行必须是说话人对白或 <action>，纯无前缀行拒绝；
    - 空行忽略。
    """
    match = _DIALOGUE_PREFIX_RE.match(str(original or "").strip())
    speaker = ""
    if match:
        candidate = match.group(1).strip()
        if candidate and candidate not in _NON_SPEAKER_LABELS:
            speaker = candidate
    lines = str(text or "").split("\n")
    first = lines[0].strip() if lines else ""
    if speaker and not re.match(rf"^{re.escape(speaker)}(?:[（(][^）)]*[）)])?[:：]", first):
        return False
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("<action"):
            continue
        line_match = _DIALOGUE_PREFIX_RE.match(stripped)
        if not line_match:
            # 无说话人前缀的续行会被分镜直接跳过、不生成 unit，等于丢内容
            return False
        line_speaker = line_match.group(1).strip()
        if not line_speaker or line_speaker in _NON_SPEAKER_LABELS:
            return False
        if speaker and line_speaker != speaker:
            return False
    return True


def normalize_review(raw: Any, episodes: Any) -> Optional[Dict[str, Any]]:
    """规范化模型质检结果：只保留可定位、可执行、可回滚的建议。

    - 集号 / 行号必须真实存在；`line` 为集内 1-based 行号；
    - `replace` / `delete` 同一行只保留第一条，`insert_after` 可多条；
    - 每条建议分配稳定序号 id，供前端逐条勾选与应用。
    """
    if not isinstance(raw, dict):
        return None
    summary = _clean_text(raw.get("summary"), 500)
    raw_issues = raw.get("issues") if isinstance(raw.get("issues"), list) else []
    episodes_by_number = episode_map(episodes)
    issues: List[Dict[str, Any]] = []
    seen_replace = set()
    insert_counts: Dict[tuple, int] = {}

    for item in raw_issues:
        if len(issues) >= MAX_REVIEW_ISSUES:
            break
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("episode_number"))
            line_no = int(item.get("line"))
        except (TypeError, ValueError):
            continue
        episode = episodes_by_number.get(number)
        if episode is None or line_no < 1:
            continue
        lines = episode_lines(episode.get("content"))
        if line_no > len(lines):
            continue

        problem = _clean_text(item.get("problem"), 1000)
        if not problem:
            continue
        action = str(item.get("action") or "").strip().lower()
        text = "" if action == "delete" else _clean_text(item.get("text"), MAX_ISSUE_TEXT)
        if action not in REVIEW_ACTIONS:
            # 模型偶尔省略 action：有改写文本按 replace 处理，无文本则不收
            if not text:
                continue
            action = "replace"
        if action != "delete" and not text.strip():
            continue

        # 防御：仅全/半角或兼容字符差异的 replace 一律丢弃，
        # 避免 safe_content 之类的预处理把整篇标点改写成半角。
        original_line = lines[line_no - 1]
        # 空转建议：改了但完全没改，应用后会触发无意义的下游 reconfirm
        if action == "replace" and text == original_line:
            continue
        if action == "replace" and text != original_line and (
            _compat_normalized(text) == _compat_normalized(original_line)
        ):
            continue

        # 元信息/制作标记/空行不参与质检；避免删除时长、标题、字幕或空行
        if _is_protected_line(original_line):
            continue

        # 行类型前缀保真：字幕/音效/特效/定帧/场景标题不得被换成对白等其他类型
        kind = _line_kind(original_line)
        if kind and action == "replace" and not _keeps_prefix_kind(text, kind):
            continue
        # 字幕/音效/特效/定帧/场景标题不得建议删除（只允许调整文字，不允许移除内容）
        if action == "delete" and kind:
            continue

        # 续行保真：对白续行需同说话人；动作行续行需可被分镜识别的对白或 <action>
        if action == "replace" and not kind and not _keeps_replace_continuations(original_line, text):
            continue

        # 内容接地：以原行为参照；对白 ≥25%，动作/描述行 ≥65%。
        if action == "replace" and not kind and not _is_grounded_replace(original_line, text):
            continue

        # 新增制作标记（【画面描述】、【场景：、字幕：等）一律拒绝
        if _adds_new_marker(original_line, text):
            continue

        if action == "replace":
            # 单行拆分不得超过 3 段，避免同一台词被拆成过多 unit
            if len([line for line in text.split("\n") if line.strip()]) > MAX_REPLACE_LINES:
                continue
        elif action == "insert_after":
            # insert_after 必须能在原行找到来源；每行最多 2 条，避免建议爆炸
            key = (number, line_no)
            if insert_counts.get(key, 0) >= MAX_INSERT_AFTER_PER_LINE:
                continue
            if not _is_grounded_insert(original_line, text):
                continue
            insert_counts[key] = insert_counts.get(key, 0) + 1

        key = (number, line_no)
        if action in ("replace", "delete"):
            if key in seen_replace:
                continue
            seen_replace.add(key)

        issue_type = str(item.get("type") or "").strip().lower()
        severity = str(item.get("severity") or "").strip().lower()
        issues.append(
            {
                "episode_number": number,
                "line": line_no,
                "type": issue_type if issue_type in REVIEW_TYPES else "content",
                "severity": severity if severity in REVIEW_SEVERITIES else "medium",
                "problem": problem,
                "suggestion": _clean_text(item.get("suggestion"), 1000),
                "action": action,
                "original": original_line,
                "text": text,
            }
        )

    issues.sort(key=lambda item: (item["episode_number"], item["line"]))
    for index, issue in enumerate(issues, 1):
        issue["id"] = f"qr_{index}"
    if not summary and not issues:
        # 模型明确返回空 issues 是合法的“未发现问题”；但若原始 issues 非空却
        # 全部不可定位，说明输出不可用，交给调用方重试而不是谎报干净。
        if isinstance(raw.get("issues"), list) and not raw_issues:
            return {"summary": "", "issues": []}
        return None
    return {"summary": summary, "issues": issues}


def select_issues(preview: Any, selected_ids: Any) -> List[Dict[str, Any]]:
    """按前端勾选结果筛选建议；未知 id 忽略，顺序保持预览原顺序。"""
    if not isinstance(preview, dict):
        return []
    wanted = {str(value) for value in (selected_ids or []) if str(value).strip()}
    if not wanted:
        return []
    issues = preview.get("issues")
    return [
        issue
        for issue in (issues or [])
        if isinstance(issue, dict) and str(issue.get("id")) in wanted
    ]


def apply_issue_edits(episodes: Any, issues: Any) -> List[Dict[str, Any]]:
    """把选中的建议应用到正文副本；未选中行逐字保持不变。

    先按集聚合，再把同一行的 replace/delete 与 insert_after 合并成一次线性重建，
    避免逐条字符串替换带来的偏移与二次匹配问题。
    """
    selected = [issue for issue in (issues or []) if isinstance(issue, dict)]
    if not selected:
        raise ValueError("请至少选择一条质检建议")

    updated = copy.deepcopy(list(episodes or []))
    episodes_by_number = episode_map(updated)
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for issue in selected:
        try:
            number = int(issue.get("episode_number"))
            line_no = int(issue.get("line"))
        except (TypeError, ValueError):
            continue
        if number not in episodes_by_number:
            continue
        grouped.setdefault(number, []).append(issue)

    applied = 0
    for index, episode in enumerate(updated):
        if not isinstance(episode, dict):
            continue
        number = episode_number(episode, index)
        ops = grouped.get(number)
        if not ops:
            continue
        lines = episode_lines(episode.get("content"))
        replace_ops: Dict[int, Dict[str, Any]] = {}
        insert_ops: Dict[int, List[Dict[str, Any]]] = {}
        for op in ops:
            try:
                line_no = int(op.get("line"))
                action = str(op.get("action") or "").strip().lower()
            except (TypeError, ValueError):
                continue
            if line_no < 1 or line_no > len(lines) or action not in REVIEW_ACTIONS:
                continue
            original = op.get("original")
            if original is not None and str(original) != lines[line_no - 1]:
                raise ValueError(
                    f"第 {number} 集第 {line_no} 行内容已变化，质检结果已过期，请重新质检"
                )
            if action == "insert_after":
                insert_ops.setdefault(line_no, []).append(op)
            elif line_no not in replace_ops:
                replace_ops[line_no] = op

        new_lines: List[str] = []
        for line_no, line in enumerate(lines, 1):
            replacement = replace_ops.get(line_no)
            if replacement is None:
                new_lines.append(line)
            else:
                applied += 1
                if replacement.get("action") == "replace":
                    new_lines.extend(str(replacement.get("text") or "").split("\n"))
            for op in insert_ops.get(line_no, []):
                applied += 1
                new_lines.extend(str(op.get("text") or "").split("\n"))
        episode["content"] = "\n".join(new_lines)

    if applied == 0:
        raise ValueError("没有可应用的质检建议，请重新质检")
    return updated
