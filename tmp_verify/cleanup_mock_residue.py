"""清理 mock 验证残留：api-mock 供应商与 m-pf-* 自定义模型（走正规保存通道）。"""
import copy
import sys

sys.path.insert(0, "/app")
import config as config_module  # noqa: E402
from config import Config  # noqa: E402

values = copy.deepcopy(config_module._FILE_CONFIG_VALUES)
providers = values.get("api_providers", {})
removed_provider = providers.pop("api-mock", None)
models = values.get("custom_models", [])
kept = [m for m in models if m.get("provider") != "api-mock"]
removed_models = len(models) - len(kept)
values["custom_models"] = kept

Config.update_config(values)

print(f"removed provider api-mock: {bool(removed_provider)}")
print(f"removed models: {removed_models}")
print(f"providers now: {sorted(Config.CONFIG.get('api_providers', {}).keys())}")
print(f"custom_models now: {[m.get('id') for m in Config.CONFIG.get('custom_models', [])]}")