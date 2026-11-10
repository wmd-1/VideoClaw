"""参考图阶段在 reference 模式下的跳过标记（openspec: reference-mode-skip-scene-images D1）。

`skip` 是产物内的**单一嵌套对象**：`{reason, note, mode}`。不在产物顶层新增任何标量键，
否则产物写入接口会把“全是标量的键集合”判定为选择映射格式并清空 body。

agent 短路与模式切换重建 MUST 共用本模块的构造来源，两条路径的文案才不会分叉。
"""

from typing import Any, Dict

# 机器可读原因键（前端不解析文案，只展示 note）
SKIP_REASON = "reference_mode_uses_asset_selection"

# 面向用户的说明：替代素材来源必须写明，避免被理解为“漏跑了阶段”
SKIP_NOTE = (
    "参考图生视频模式直接使用「角色/场景设计」阶段的选中素材，本阶段不生成分镜图。"
)


def build_reference_skip(mode: str = "reference") -> Dict[str, str]:
    """构造跳过标记（agent 短路与模式切换重建共用的唯一来源）。"""
    return {"reason": SKIP_REASON, "note": SKIP_NOTE, "mode": str(mode or "reference")}


def reference_skip_marker(artifact: Any) -> Dict[str, str]:
    """读取产物中的跳过标记；不是对象/缺失时返回空字典。"""
    if not isinstance(artifact, dict):
        return {}
    marker = artifact.get("skip")
    return marker if isinstance(marker, dict) else {}
