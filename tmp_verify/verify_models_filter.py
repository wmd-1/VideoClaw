"""验证 /api/models 仅返回可用模型（未配置的内置模型被过滤，补齐凭据后自动回归）。

M-f（2026-09-30）：断言由"非空且 ⊆ custom_ids"恢复为**原始配置推导的动态精确期望**
（相等断言），环境自适应与精度兼得；同时新增多能力 AND（I3）与空集精确断言。
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, "/app")
import yaml  # noqa: E402
import config as config_module  # noqa: E402
from config import CONFIG_PATH  # noqa: E402
from models.config_model import model_availability  # noqa: E402

BASE = f"http://127.0.0.1:{os.environ.get('BACKEND_PORT', '8000')}"  # 容器内外同端口
failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode())


custom_ids = {m["id"] for m in get("/api/config")["config"]["custom_models"]}

# 动态期望推导：直接读原始 config.yaml（与 verify_prune 同法），不 import 被测过滤路径
raw = yaml.safe_load(CONFIG_PATH.open(encoding="utf-8")) or {}
custom_raw = raw.get("custom_models") or []


def media_of(types):
    if "video" in (types or []):
        return "video"
    if {"t2i", "i2i"} & set(types or []):
        return "image"
    return None


def abilities_of(m):
    """自定义视频模型未声明 abilities 时的默认（与能力推导默认一致）。"""
    return (m.get("abilities") or []) or (["text_to_video", "first_frame_i2v"] if media_of(m.get("types")) == "video" else [])


for mt in ("llm", "vlm", "t2i", "i2i", "video"):
    expected = {m["id"] for m in custom_raw if mt in (m.get("types") or [])}
    ids = {m["id"] for m in get(f"/api/models?model_type={mt}")["models"]}
    check(
        f"model_type={mt} 精确等于配置声明（无凭据的内置被过滤）",
        ids == expected and bool(ids or True),
        f"ids={sorted(ids)} expected={sorted(expected)}",
    )
    check(f"model_type={mt} 不泄漏非 custom 模型", ids <= custom_ids, str(sorted(ids - custom_ids))[:80])

ids = {m["id"] for m in get("/api/models")["models"]}
expected_all = {m["id"] for m in custom_raw if media_of(m.get("types"))}
check("媒体工作流全量列表精确等于声明集合", ids == expected_all, f"ids={sorted(ids)} expected={sorted(expected_all)}")

ids = {m["id"] for m in get("/api/models?media_type=video&ability=first_frame_i2v&verified_only=true")["models"]}
expected_ff = {m["id"] for m in custom_raw
               if media_of(m.get("types")) == "video"
               and "first_frame_i2v" in abilities_of(m)
               and bool(m.get("api_contract_verified", True))}
check("Pipeline 查询（first_frame_i2v）精确等于能力声明集合", ids == expected_ff, f"ids={sorted(ids)} expected={sorted(expected_ff)}")

# I3 回归：多能力 AND——查询 audio_reference,video_reference 仅返回双声明模型
ids_and = {m["id"] for m in get("/api/models?media_type=video&ability=audio_reference,video_reference")["models"]}
expected_and = {m["id"] for m in custom_raw
                if media_of(m.get("types")) == "video"
                and {"audio_reference", "video_reference"} <= set(abilities_of(m))}
check("多能力 AND（audio+video_reference）精确交集", ids_and == expected_and, f"ids={sorted(ids_and)} expected={sorted(expected_and)}")
single_a = {m["id"] for m in get("/api/models?media_type=video&ability=audio_reference")["models"]}
check("单能力并集 ⊇ AND 交集（语义方向正确）", ids_and <= single_a, f"and={sorted(ids_and)} single={sorted(single_a)}")

# 联动：补齐内置凭据后自动回归列表（进程内模拟，不影响运行中的服务）
before = model_availability("qwen3.5-plus")
prov = config_module.Config.CONFIG["api_providers"]["dashscope"]
old_key = prov.get("api_key")
prov["api_key"] = "test-key"
config_module.Config.DASHSCOPE_API_KEY = "test-key"
after = model_availability("qwen3.5-plus")
prov["api_key"] = old_key
config_module.Config.DASHSCOPE_API_KEY = old_key or ""
check("内置缺 Key 时不可用（将被过滤）", not before.get("available"), str(before))
check("补齐凭据后可用（自动回归列表）", after.get("available"), str(after))

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)
