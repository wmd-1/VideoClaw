"""任务 8.2 回归（模块级）：遍历内置注册表全部模型 id，断言 resolve 均命中 builtin（无回归）。"""

import sys

from models.config_model import MODEL_CONFIG, resolve_model_entry

failures = []
for model_id in MODEL_CONFIG.get("models", {}).keys():
    kind, _ = resolve_model_entry(model_id)
    if kind != "builtin":
        failures.append((model_id, kind))
    else:
        print(f"[PASS] builtin resolve: {model_id}")

print(f"\n共 {len(MODEL_CONFIG.get('models', {}))} 个内置模型")
if failures:
    print("FAILED:", failures)
    sys.exit(1)
print("ALL PASS (builtin regression)")