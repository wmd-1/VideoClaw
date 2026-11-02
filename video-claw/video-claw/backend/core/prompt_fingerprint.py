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


def sha_of(value: Any) -> str:
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_payload(
    *,
    shot_content: Any,
    duration: Any,
    mode: Any,
    references: Sequence[Tuple[str, str, Any]],
    style: Any = "",
    facts: Iterable[Dict[str, Any]] = (),
    grounding_model: Any = "",
) -> Dict[str, Any]:
    """指纹 payload（供整体哈希与逐维度哈希共用）。"""
    return {
        "shot_content": str(shot_content or "").strip(),
        "duration": int(duration or 0),
        "mode": str(mode or ""),
        "references": [[str(container), str(key), media_ref(path)] for container, key, path in references],
        "style": str(style or "").strip(),
        "facts": normalize_facts(facts),
        "grounding_model": str(grounding_model or "").strip(),
    }


def dimension_hashes(payload: Dict[str, Any]) -> Dict[str, str]:
    """逐维度哈希，供 stale_reasons 结构化枚举使用。"""
    return {key: sha_of(value) for key, value in payload.items()}


def build_input_fingerprint(
    *,
    shot_content: Any,
    duration: Any,
    mode: Any,
    references: Sequence[Tuple[str, str, Any]],
    style: Any = "",
    facts: Iterable[Dict[str, Any]] = (),
    grounding_model: Any = "",
) -> str:
    """grounding 只纳入**模型标识**：描述文本是 (参考图, 模型, 策略) 的派生结果，
    参考图已在 references、模型属配置；把文本哈希塞进指纹会导致解析端无法重建、
    且 VLM 非确定性会造成每次都 stale。"""
    payload = build_payload(
        shot_content=shot_content, duration=duration, mode=mode, references=references,
        style=style, facts=facts, grounding_model=grounding_model,
    )
    return sha_of(payload)


__all__ = [
    "build_input_fingerprint",
    "build_payload",
    "dimension_hashes",
    "media_ref",
    "normalize_facts",
    "sha_of",
]
