"""任务组 1 验证脚本：config 归一化 / env 覆盖层 / 保存剥离 / 硬校验。

在既有 Docker 容器内运行（标准输入注入，避免写宿主机 data/ 目录）：
  docker exec -i -w /app -e PYTHONPATH=/app video-claw-backend python - \
    < tmp_verify/verify_config_layer.py
"""
import copy
import sys

import config as cfg

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


# ── 1.1 归一化：缺 id 跳过 / 按 id 去重保留后者 / types 字符串拆分 / concurrency 转 int ──
normalized = cfg._normalize_custom_models([
    {"id": "a", "provider": "p1", "types": ["t2i"]},
    {"provider": "no-id", "types": ["llm"]},
    "not-a-dict",
    {"id": "a", "provider": "p2", "types": "i2i, t2i", "concurrency": "2"},
])
check("1.1 归一化仅保留 1 条（去重 + 跳过非法）", len(normalized) == 1, str(normalized))
check("1.1 去重保留后者内容", normalized and normalized[0]["provider"] == "p2")
check("1.1 types 字符串拆分为列表", normalized and normalized[0]["types"] == ["i2i", "t2i"])
check("1.1 concurrency 转为 int", normalized and normalized[0]["concurrency"] == 2)

# ── 1.1 sanitize：引用不存在/内置/缺 types/供应商不完整的条目在加载期被丢弃 ──
sanitized = cfg._sanitize_config({
    "api_providers": {
        "common": {},
        "cmp": {"protocol": "openai", "base_url": "http://x/v1"},
        "broken": {"protocol": "", "base_url": ""},
    },
    "custom_models": [
        {"id": "ok", "provider": "cmp", "types": ["llm"]},
        {"id": "missing-provider", "provider": "nope", "types": ["llm"]},
        {"id": "builtin-provider", "provider": "dashscope", "types": ["llm"]},
        {"id": "empty-types", "provider": "cmp", "types": []},
        {"id": "broken-provider", "provider": "broken", "types": ["llm"]},
    ],
})
check(
    "1.1 sanitize 仅保留合法条目",
    [e["id"] for e in sanitized["custom_models"]] == ["ok"],
    str([e["id"] for e in sanitized["custom_models"]]),
)

# ── 1.3 env 覆盖层（monkeypatch env 来源，不触碰真实环境）──
base_view = cfg._coerce_config({
    "api_providers": {
        "omni-8091": {"protocol": "vllm-omni", "base_url": "http://file-host:8091/v1", "api_key": ""},
    },
    "models": {"llm": "qwen3.5-plus"},
    "custom_models": [
        {"id": "local-image", "provider": "omni-8091", "types": ["t2i", "i2i"]},
        {"id": "merge-me", "provider": "omni-8091", "types": ["t2i"], "abilities": ["text_to_image"]},
    ],
})
env_stub = {
    "VC_PROVIDER_OMNI_8091__BASE_URL": "http://env-host:8091/v1",
    "VC_PROVIDER_OMNI_8091__API_KEY": "sk-env",
    "VC_PROVIDER_NEW_SRV__PROTOCOL": "sglang",
    "VC_PROVIDER_NEW_SRV__BASE_URL": "http://127.0.0.1:30010/v1",
    "VC_PROVIDER_BAD_SRV__PROTOCOL": "nope",
    "VC_PROVIDER_BAD_SRV__BASE_URL": "http://127.0.0.1:9/v1",
    "VC_MODEL_LLM": "merge-me",
    "VC_CUSTOM_MODEL_1__ID": "merge-me",
    "VC_CUSTOM_MODEL_1__TYPES": "t2i,video",
    "VC_CUSTOM_MODEL_1__CONCURRENCY": "2",
    "VC_CUSTOM_MODEL_2__ID": "env-only",
    "VC_CUSTOM_MODEL_2__PROVIDER": "new_srv",
    "VC_CUSTOM_MODEL_2__TYPES": "t2i",
    "VC_CUSTOM_MODEL_3__PROVIDER": "new_srv",  # 缺 ID → 跳过
    "VC_MODEL_TYPO_KEY": "x",  # 未识别 → 跳过
}
original_loader = cfg._load_env_sources
cfg._load_env_sources = lambda: env_stub
try:
    effective, env_info = cfg._apply_env_overrides(base_view)
finally:
    cfg._load_env_sources = original_loader

check(
    "1.3 已有供应商字段被 env 覆盖",
    effective["api_providers"]["omni-8091"]["base_url"] == "http://env-host:8091/v1",
)
check(
    "1.3 provider 键大小写/横杠归一命中已有条目",
    "omni-8091" in effective["api_providers"] and "omni_8091" not in effective["api_providers"],
)
check(
    "1.3 env 新增合法供应商",
    effective["api_providers"].get("new_srv", {}).get("protocol") == "sglang",
)
check("1.3 非法 env 供应商被跳过", "bad_srv" not in effective["api_providers"])
check("1.3 VC_MODEL_* 覆盖默认模型", effective["models"]["llm"] == "merge-me")
merged = next(e for e in effective["custom_models"] if e["id"] == "merge-me")
check(
    "1.3 同 id 模型逐字段合并（env 优先）",
    merged["types"] == ["t2i", "video"] and merged.get("concurrency") == 2,
    str(merged),
)
check("1.3 env 新增模型条目", any(e["id"] == "env-only" for e in effective["custom_models"]))
check(
    "1.3 env 信息列出覆盖字段",
    "api_providers.omni-8091.base_url" in env_info["fields"]
    and "models.llm" in env_info["fields"]
    and "custom_models[merge-me].types" in env_info["fields"],
    str(env_info["fields"]),
)
check(
    "1.3 env 信息列出 env-only 条目",
    env_info["providers"] == ["new_srv"] and env_info["models"] == ["env-only"],
    str(env_info),
)

# ── 1.4 保存剥离：env 覆盖字段恢复文件原值；env-only 条目不写回 ──
page_values = copy.deepcopy(effective)  # 模拟设置页提交“有效配置”
page_values["api_providers"]["omni-8091"]["base_url"] = "http://page-edit:8091/v1"  # 页面改了也应被剥离
file_view = cfg._strip_env_overridden(page_values, env_info, base_view)
check(
    "1.4 被覆盖字段恢复文件原值",
    file_view["api_providers"]["omni-8091"]["base_url"] == "http://file-host:8091/v1",
)
check("1.4 env-only 供应商不写回", "new_srv" not in file_view["api_providers"])
check("1.4 env-only 模型不写回", all(e["id"] != "env-only" for e in file_view["custom_models"]))
check("1.4 模型覆盖字段恢复文件原值", file_view["models"]["llm"] == "qwen3.5-plus")
check(
    "1.4 同 id 模型被覆盖字段恢复文件原值",
    next(e for e in file_view["custom_models"] if e["id"] == "merge-me")["types"] == ["t2i"],
)


# ── 1.2 硬校验 ──
def expect_error(name, payload, keyword):
    try:
        cfg._validate_effective_config(payload)
        check(name, False, "未抛出 ValueError")
    except ValueError as exc:
        check(name, keyword in str(exc), str(exc))


expect_error(
    "1.2 非法 protocol 被拒绝",
    cfg._coerce_config({"api_providers": {"srv": {"protocol": "bad", "base_url": "http://x/v1"}}, "models": {}}),
    "protocol",
)
expect_error(
    "1.2 缺 provider 被拒绝",
    cfg._coerce_config({"models": {}, "custom_models": [{"id": "m1", "types": ["llm"]}]}),
    "缺少 provider",
)
expect_error(
    "1.2 引用不存在供应商被拒绝",
    cfg._coerce_config({"models": {}, "custom_models": [{"id": "m1", "provider": "ghost", "types": ["llm"]}]}),
    "引用的供应商不存在",
)
expect_error(
    "1.2 引用内置供应商被拒绝",
    cfg._coerce_config({"models": {}, "custom_models": [{"id": "m1", "provider": "dashscope", "types": ["llm"]}]}),
    "内置供应商",
)
expect_error(
    "1.2 types 为空被拒绝",
    cfg._coerce_config({
        "api_providers": {"srv": {"protocol": "openai", "base_url": "http://127.0.0.1:1/v1"}},
        "models": {},
        "custom_models": [{"id": "m1", "provider": "srv", "types": []}],
    }),
    "types 不能为空",
)
valid_payload = cfg._coerce_config({
    "api_providers": {"srv": {"protocol": "openai", "base_url": "http://127.0.0.1:1/v1"}},
    "models": {},
    "custom_models": [{"id": "m1", "provider": "srv", "types": ["llm"], "concurrency": 2}],
})
try:
    cfg._validate_effective_config(valid_payload)
    check("1.2 合法配置通过校验", True)
except ValueError as exc:
    check("1.2 合法配置通过校验", False, str(exc))

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} 项未通过: {FAILURES}")
    sys.exit(1)
print("ALL PASS")