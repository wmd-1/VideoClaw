"""验证多能力 AND 交集过滤（I3，评审加固）。

覆盖：
- list_api_models(required=['audio_reference','video_reference']) 仅返回同时声明两项的模型；
- 单能力查询与旧行为等价（含该能力的模型均返回）；
- _split_abilities 对逗号分隔/单值/空的解析（向后兼容 ability 参数）。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_multi_ability_filter.py
"""
import os
import sys

sys.path.insert(0, "/app")
if os.environ.get("VC_PATCHED_BACKEND"):
    sys.path.insert(0, os.environ["VC_PATCHED_BACKEND"])

import models.config_model as cm  # noqa: E402
from api.routers.pipelines import _split_abilities  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


def rec(mid, adapter):
    return {"model": mid, "adapter_ability_types": adapter, "ability_types": adapter, "type": ["video"]}


RECORDS = {
    "audio_only": rec("audio_only", ["first_frame_i2v", "audio_reference"]),
    "video_only": rec("video_only", ["first_frame_i2v", "video_reference"]),
    "both": rec("both", ["first_frame_i2v", "audio_reference", "video_reference"]),
    "plain": rec("plain", ["first_frame_i2v"]),
}


def fake_model_records(media_type=None):
    return list(RECORDS.values())


original_records = cm.model_records
cm.model_records = fake_model_records
try:
    # 1) 多能力 AND：仅 both
    both = cm.list_api_models(media_type="video", required_adapter_abilities=["audio_reference", "video_reference"])
    ids = {r["model"] for r in both}
    check("音频+视频参考 AND 交集仅含双能力模型", ids == {"both"}, f"ids={sorted(ids)}")

    # 2) 单能力 audio_reference：audio_only + both
    audio = cm.list_api_models(media_type="video", required_adapter_abilities=["audio_reference"])
    ids_a = {r["model"] for r in audio}
    check("单能力 audio_reference 命中 audio_only+both", ids_a == {"audio_only", "both"}, f"ids={sorted(ids_a)}")

    # 3) 单能力 first_frame_i2v：全部（与旧 intersection 行为等价）
    ff = cm.list_api_models(media_type="video", required_adapter_abilities=["first_frame_i2v"])
    ids_f = {r["model"] for r in ff}
    check("单能力 first_frame_i2v 命中全部", ids_f == {"audio_only", "video_only", "both", "plain"}, f"ids={sorted(ids_f)}")

    # 4) 三能力 AND（含不存在的组合）→ 空
    none = cm.list_api_models(media_type="video", required_adapter_abilities=["audio_reference", "video_reference", "high_quality"])
    check("三能力 AND 无匹配返回空", len(none) == 0, f"n={len(none)}")

    # 5) _split_abilities 解析（向后兼容单能力）
    check("逗号分隔解析为列表", _split_abilities("audio_reference,video_reference") == ["audio_reference", "video_reference"])
    check("单值解析为单元素列表", _split_abilities("first_frame_i2v") == ["first_frame_i2v"])
    check("空值解析为 None（不过滤）", _split_abilities(None) is None and _split_abilities("") is None)
    check("含空白与尾逗号稳健", _split_abilities(" audio_reference , video_reference ,") == ["audio_reference", "video_reference"])
finally:
    cm.model_records = original_records

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)
