"""真实进程环境变量覆盖验证（任务 1.3 补充）。

运行方式（示例）：
  docker exec -i -w /app -e PYTHONPATH=/app \
    -e VC_PROVIDER_NEW_SRV__PROTOCOL=sglang \
    -e VC_PROVIDER_NEW_SRV__BASE_URL=http://127.0.0.1:30010/v1 \
    video-claw-backend python - < tmp_verify/verify_env_process.py
"""
import sys

import config as cfg

base_url = cfg.Config.CONFIG["api_providers"].get("new_srv", {}).get("base_url")
fields = cfg.Config.ENV_OVERRIDES["fields"]
providers = cfg.Config.ENV_OVERRIDES["providers"]
ok = (
    base_url == "http://127.0.0.1:30010/v1"
    and "api_providers.new_srv.base_url" in fields
    and "api_providers.new_srv.protocol" in fields
    and providers == ["new_srv"]
)
print("ENV-EXEC", "PASS" if ok else "FAIL", "base_url=", base_url, "fields=", fields)
sys.exit(0 if ok else 1)