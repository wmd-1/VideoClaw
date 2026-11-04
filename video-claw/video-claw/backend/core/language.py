"""会话语言单点解析（C2/Q4）。

优先级：显式用户设置 > 显式指令（用英文/用中文）> 正文主导比例 ≥80%
> 标题主导比例 ≥80% > 当前值（ambiguous）> 默认 zh。
只做纯计算；持久化由调用方在写路径完成，读接口不产生副作用。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

LANGUAGE_ZH = "zh"
LANGUAGE_EN = "en"
SOURCE_USER = "user"
SOURCE_INSTRUCTION = "instruction"
SOURCE_BODY = "body"
SOURCE_TITLE = "title"
SOURCE_DEFAULT = "default"
SOURCE_AMBIGUOUS = "ambiguous"

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")
_ZH_WORDS = re.compile(r"用中文|使用中文|中文输出|中文撰写|中文剧本")
_EN_WORDS = re.compile(
    r"用英文|用英语|使用英文|英文输出|英文撰写|英文剧本|in english|english\s+(?:script|output|version)",
    re.I,
)
_MIN_SAMPLE = 10
_DOMINANCE = 0.8


def normalize_language(value: Any) -> Optional[str]:
    """把 zh/en/中文/English 等写法归一为 zh/en；无法识别返回 None。"""
    text = str(value or "").strip().lower()
    if not text:
        return None
    if re.match(r"^zh(?:[-_]|$)", text) or "中文" in text or "汉语" in text:
        return LANGUAGE_ZH
    if re.match(r"^en(?:[-_]|$)", text) or text == "english" or "英文" in text or "英语" in text:
        return LANGUAGE_EN
    return None


def _dominant(text: str) -> Optional[str]:
    cjk = len(_CJK.findall(text or ""))
    latin = len(_LATIN.findall(text or ""))
    total = cjk + latin
    if total < _MIN_SAMPLE:
        return None
    ratio = cjk / total
    if ratio >= _DOMINANCE:
        return LANGUAGE_ZH
    if ratio <= 1 - _DOMINANCE:
        return LANGUAGE_EN
    return None


def resolve_language(
    *,
    explicit: Any = None,
    instruction: str = "",
    body: str = "",
    title: str = "",
    current: Any = None,
) -> Dict[str, str]:
    """返回 `{language, source}`；混合语言无法判定时保留 current 并标 ambiguous。"""
    chosen = normalize_language(explicit)
    if chosen:
        return {"language": chosen, "source": SOURCE_USER}

    text = str(instruction or "")
    has_zh = bool(_ZH_WORDS.search(text))
    has_en = bool(_EN_WORDS.search(text))
    if has_en and not has_zh:
        return {"language": LANGUAGE_EN, "source": SOURCE_INSTRUCTION}
    if has_zh and not has_en:
        return {"language": LANGUAGE_ZH, "source": SOURCE_INSTRUCTION}

    for value, source in ((body, SOURCE_BODY), (title, SOURCE_TITLE)):
        judged = _dominant(value)
        if judged:
            return {"language": judged, "source": source}

    kept = normalize_language(current)
    if kept:
        return {"language": kept, "source": SOURCE_AMBIGUOUS}
    return {"language": LANGUAGE_ZH, "source": SOURCE_DEFAULT}


def language_from_meta(meta: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(meta, dict):
        return None
    return normalize_language(meta.get("language"))


__all__ = [
    "LANGUAGE_ZH",
    "LANGUAGE_EN",
    "SOURCE_USER",
    "SOURCE_INSTRUCTION",
    "SOURCE_BODY",
    "SOURCE_TITLE",
    "SOURCE_DEFAULT",
    "SOURCE_AMBIGUOUS",
    "normalize_language",
    "resolve_language",
    "language_from_meta",
]
