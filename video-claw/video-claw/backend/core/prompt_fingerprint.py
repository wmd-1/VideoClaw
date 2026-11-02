"""H3 输入指纹（Q1 前置数据）。

指纹覆盖“当次改写的语义输入”：镜头正文、有效时长、模式、有序参考集、风格、
人物/场景权威事实。算法为归一化 JSON（键排序、紧凑分隔、UTF-8）后 SHA-256。

约束：
- 不纳入时间戳、临时 job id；
- 媒体引用用 public path / media id；本地绝对路径只取 basename，避免换机器就失效；
- 参考集顺序即视频入参顺序（场景→角色），换图/换版本必须改变指纹。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Sequence, Tuple

_FACT_FIELDS = ("kind", "id", "name", "description", "species", "location")


def media_ref(value: Any) -> str:
    """媒体身份：**以 public_path 为准**，不随本地缓存形态变化。

    - `ResolvedMedia` 自带 canonical `public_path`（可能是本地 `_refs` 路径 + public 属性）；
    - 纯字符串 public path 原样；本地绝对路径取 basename（兜底）；
    - http(s) 去掉 query（presigned 签名每次不同）。
    """
    public = str(getattr(value, "public_path", "") or "").strip()
    text = public or str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text.split("?", 1)[0]
    if text.startswith("/code/"):
        return text
    return text.rsplit("/", 1)[-1]


def normalize_facts(facts: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    """人物/场景权威事实归一化：固定字段集 + 按 (kind, id/name) 排序。"""
    cleaned: List[Dict[str, str]] = []
    for item in facts or []:
        if not isinstance(item, dict):
            continue
        cleaned.append({field: str(item.get(field) or "").strip() for field in _FACT_FIELDS})
    return sorted(cleaned, key=lambda entry: (entry["kind"], entry["id"] or entry["name"]))


def build_input_fingerprint(
    *,
    shot_content: Any,
    duration: Any,
    mode: Any,
    references: Sequence[Tuple[str, str, Any]],
    style: Any = "",
    facts: Iterable[Dict[str, Any]] = (),
    grounding_model: Any = "",
    grounding_text: Any = "",
) -> str:
    """grounding 是实际提示词的一部分：模型与描述文本哈希都纳入指纹。"""
    grounding_blob = str(grounding_text or "").strip()
    payload = {
        "shot_content": str(shot_content or "").strip(),
        "duration": int(duration or 0),
        "mode": str(mode or ""),
        "references": [[str(container), str(key), media_ref(path)] for container, key, path in references],
        "style": str(style or "").strip(),
        "facts": normalize_facts(facts),
        "grounding": {
            "model": str(grounding_model or "").strip(),
            "text_hash": hashlib.sha256(grounding_blob.encode("utf-8")).hexdigest() if grounding_blob else "",
        },
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


__all__ = ["build_input_fingerprint", "media_ref", "normalize_facts"]
