import copy
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
CONFIG_EXAMPLE_PATH = BASE_DIR / "config.yaml.example"

# 内置供应商：沿用既有按名称路由的客户端；其余 api_providers 条目视为“自定义供应商”
BUILTIN_PROVIDERS = ("openai", "gemini", "deepseek", "dashscope", "ark", "kling")
# 自定义供应商可声明的接口协议（决定走哪个协议适配器）
CUSTOM_PROTOCOLS = ("openai", "vllm-omni", "sglang")
# 自定义模型可声明的模型类型
CUSTOM_MODEL_TYPES = ("llm", "vlm", "t2i", "i2i", "video")

DEFAULT_CONFIG: Dict[str, Any] = {
    "project_name": "Video-Claw",
    "server": {
        "host": "127.0.0.1",
        "port": 8000,
        "log_level": "INFO",
        "access_log": False,
    },
    "api_providers": {
        "common": {
            "print_model_input": False,
            "proxy": "",
        },
        "openai": {
            "api_key": "",
            "base_url": "https://api.openai.com/v1",
            "enable_proxy": False,
        },
        "gemini": {
            "api_key": "",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "enable_proxy": False,
        },
        "deepseek": {
            "api_key": "",
            "base_url": "https://api.deepseek.com/v1",
            "enable_proxy": False,
        },
        "dashscope": {
            "api_key": "",
            "base_url": "https://dashscope.aliyuncs.com/api/v1",
            "enable_proxy": False,
        },
        "ark": {
            "api_key": "",
            "base_url": "https://ark.cn-beijing.volces.com/api/v3",
            "enable_proxy": False,
        },
        "kling": {
            "base_url": "https://api-beijing.klingai.com",
            "api_key": "",
            "enable_proxy": False,
        },
    },
    "models": {
        "llm": "qwen3.5-plus",
        "vlm": "qwen3.5-plus",
        "image_it2i": "doubao-seedream-5-0-260128",
        "image_t2i": "doubao-seedream-5-0-260128",
        "video": "wan2.7-i2v",
        "video_first_frame": "wan2.7-i2v",
        "video_start_end": "wan2.7-i2v",
        "video_reference": "wan2.7-r2v",
    },
    "generation": {
        "style": "realistic",
        "video_ratio": "16:9",
        "video_resolution": "720P",
        "video_generation_mode": "first_frame",
    },
    "custom_models": [],
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _get(data: Dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _coerce_config(data: Dict[str, Any]) -> Dict[str, Any]:
    clean = _deep_merge(DEFAULT_CONFIG, data)
    clean.pop("llm", None)
    raw_server = data.get("server", {}) if isinstance(data, dict) else {}

    legacy_models = data.get("models", {}) if isinstance(data, dict) else {}
    if isinstance(legacy_models, dict):
        for legacy_key in ("style", "video_ratio", "video_resolution"):
            # Legacy config compatibility: older config.yaml stored generation settings under models.*.
            if legacy_key in legacy_models and not _get(data, f"generation.{legacy_key}"):
                clean.setdefault("generation", {})[legacy_key] = legacy_models[legacy_key]
            clean["models"].pop(legacy_key, None)
        if legacy_models.get("video") and not any(
            legacy_models.get(key) for key in ("video_first_frame", "video_start_end", "video_reference")
        ):
            # Legacy config compatibility: older configs had one models.video instead of mode-specific video models.
            clean["models"]["video_first_frame"] = legacy_models["video"]
        # Legacy config compatibility: models.eval was never used by runtime agents; keep it out after load.
        clean["models"].pop("eval", None)

    server = clean["server"]
    server["host"] = str(server.get("host") or DEFAULT_CONFIG["server"]["host"])
    try:
        server["port"] = int(server.get("port"))
    except (TypeError, ValueError):
        server["port"] = DEFAULT_CONFIG["server"]["port"]
    server["log_level"] = _normalize_log_level(
        server.get("log_level") if isinstance(raw_server, dict) and "log_level" in raw_server else None,
        server.get("debug"),
    )
    server.pop("debug", None)
    server["access_log"] = _as_bool(server.get("access_log"))
    server.pop("admin_password", None)

    common = clean["api_providers"]["common"]
    for key in ("local_proxy", "http_proxy", "https_proxy"):
        common.pop(key, None)
    common["print_model_input"] = _as_bool(common.get("print_model_input"))
    common["proxy"] = str(common.get("proxy") or "")

    if isinstance(clean["models"].get("llm"), dict):
        clean["models"]["llm"] = clean["models"]["llm"].get("model") or DEFAULT_CONFIG["models"]["llm"]

    for key, value in clean["models"].items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                value[sub_key] = "" if sub_value is None else str(sub_value)
        else:
            clean["models"][key] = "" if value is None else str(value)

    for key, value in clean["generation"].items():
        clean["generation"][key] = "" if value is None else str(value)

    for provider, values in clean["api_providers"].items():
        if provider == "common":
            continue
        if not isinstance(values, dict):
            # 防御：非对象形式的供应商条目归一为空对象，避免后续访问崩溃
            values = {}
            clean["api_providers"][provider] = values
        for key, value in values.items():
            if key == "enable_proxy":
                values[key] = _as_bool(value)
            else:
                values[key] = "" if value is None else str(value)

    clean["custom_models"] = _normalize_custom_models(clean.get("custom_models"))

    return clean


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_log_level(value: Any, legacy_debug: Any = None) -> str:
    allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if legacy_debug is not None and (value is None or str(value).strip() == ""):
        return "DEBUG" if _as_bool(legacy_debug) else "INFO"
    normalized = str(value or DEFAULT_CONFIG["server"]["log_level"]).strip().upper()
    return normalized if normalized in allowed else DEFAULT_CONFIG["server"]["log_level"]


def _str_list(value: Any) -> List[str]:
    """归一化列表字段：支持 YAML 列表或逗号分隔字符串。"""
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _normalize_custom_models(value: Any) -> List[Dict[str, Any]]:
    """归一化 custom_models 列表：字段字符串化、按 id 去重保留后者、缺 id 的条目跳过。"""
    if not isinstance(value, list):
        return []
    entries: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for item in value:
        if not isinstance(item, dict):
            logger.warning("custom_models entry skipped: not a mapping")
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            logger.warning("custom_models entry skipped: missing id")
            continue
        entry: Dict[str, Any] = {
            "id": model_id,
            "provider": str(item.get("provider") or ""),
            "model": str(item.get("model") or ""),
            "types": _str_list(item.get("types")),
            "abilities": _str_list(item.get("abilities")),
        }
        if item.get("concurrency") is not None:
            try:
                entry["concurrency"] = int(item["concurrency"])
            except (TypeError, ValueError):
                logger.warning("custom_models entry %s: ignore invalid concurrency", model_id)
        if isinstance(item.get("capabilities"), dict):
            entry["capabilities"] = copy.deepcopy(item["capabilities"])
        if model_id in entries:
            logger.warning("custom_models duplicate id %s: keeping the last entry", model_id)
        else:
            order.append(model_id)
        entries[model_id] = entry
    return [entries[model_id] for model_id in order]


ENV_VC_PREFIX = "VC_"
ENV_PROVIDER_PATTERN = re.compile(
    r"^VC_PROVIDER_([A-Za-z0-9_]+)__(PROTOCOL|BASE_URL|API_KEY|ENABLE_PROXY)$"
)
ENV_CUSTOM_MODEL_PATTERN = re.compile(
    r"^VC_CUSTOM_MODEL_([A-Za-z0-9]+)__(ID|PROVIDER|MODEL|NAME|TYPES|ABILITIES|CONCURRENCY)$"
)
ENV_MODEL_KEYS = {
    "VC_MODEL_LLM": "llm",
    "VC_MODEL_VLM": "vlm",
    "VC_MODEL_IMAGE_T2I": "image_t2i",
    "VC_MODEL_IMAGE_IT2I": "image_it2i",
    "VC_MODEL_VIDEO_FIRST_FRAME": "video_first_frame",
    "VC_MODEL_VIDEO_START_END": "video_start_end",
    "VC_MODEL_VIDEO_REFERENCE": "video_reference",
}

# API Server / Common 配置的 env 键：设置页不再展示，统一走 .env / config.yaml
ENV_SERVER_KEYS = {
    "VC_SERVER__HOST": "host",
    "VC_SERVER__PORT": "port",
    "VC_SERVER__LOG_LEVEL": "log_level",
    "VC_SERVER__ACCESS_LOG": "access_log",
}
ENV_COMMON_KEYS = {
    "VC_COMMON__PROXY": "proxy",
    "VC_COMMON__PRINT_MODEL_INPUT": "print_model_input",
}
# 额外纳入收集的非 VC_ 前缀键：用于 server.host/port 与 .env 后端端口对齐
_ENV_EXTRA_KEYS = ("BACKEND_HOST", "BACKEND_PORT")


def _parse_env_file(path: Optional[Path]) -> Dict[str, str]:
    """轻量 .env 解析：KEY=VALUE、# 注释、可选 export 前缀、去成对引号（不支持多行值）。"""
    values: Dict[str, str] = {}
    if path is None or not path.exists():
        return values
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning(".env file unreadable: %s", path)
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            logger.warning(".env line ignored (no '='): %s", path)
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _repo_root_env_path() -> Optional[Path]:
    """仓库根目录 .env（backend 向上两级；容器内 /app 无此目录时返回 None）。"""
    try:
        return BASE_DIR.parents[2] / ".env"
    except IndexError:
        return None


def _load_env_sources() -> Dict[str, str]:
    """收集配置来源：仓库根 .env → backend/.env（后者覆盖前者）→ 进程环境（最高优先级）。

    包含 VC_* 键与 BACKEND_HOST/BACKEND_PORT（后者用于 server 端口对齐回退）。
    """
    merged: Dict[str, str] = {}
    for path in (_repo_root_env_path(), BASE_DIR / ".env"):
        for key, value in _parse_env_file(path).items():
            if key.startswith(ENV_VC_PREFIX) or key in _ENV_EXTRA_KEYS:
                merged[key] = value
    for key, value in os.environ.items():
        if key.startswith(ENV_VC_PREFIX) or key in _ENV_EXTRA_KEYS:
            merged[key] = value
    return merged


def _resolve_provider_key(providers: Dict[str, Any], env_name: str) -> str:
    """把 env 中的供应商名（大写、_ 分隔）映射到配置里的 provider 键。"""
    for existing in providers:
        if existing != "common" and existing.upper().replace("-", "_") == env_name:
            return existing
    return env_name.lower()


def _is_custom_provider_complete(provider_config: Any) -> bool:
    """自定义供应商是否具备可调用条件（protocol 合法且 base_url 为 http(s)）。"""
    if not isinstance(provider_config, dict):
        return False
    if provider_config.get("protocol") not in CUSTOM_PROTOCOLS:
        return False
    base_url = str(provider_config.get("base_url") or "")
    return base_url.startswith(("http://", "https://"))


def _apply_env_overrides(
    file_view: Dict[str, Any], baseline: Optional[Dict[str, Any]] = None
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """应用 VC_* 环境变量覆盖层，返回 (有效配置, env 覆盖信息)。

    env 信息结构：{"fields": [点路径...], "providers": [仅 env 引入的 provider 键...],
    "models": [仅 env 引入的模型 id...]}；非法/不完整的 env 条目会被跳过并记日志。
    `baseline` 为“文件视图”基线：仅 env 定义、而基线中不存在的条目会标记为 env-only
    （保存时不写回配置文件）。默认与 file_view 相同（启动加载场景）。
    """
    env = _load_env_sources()
    effective = copy.deepcopy(file_view)
    providers = effective.setdefault("api_providers", {})
    env_fields: List[str] = []
    env_created_providers: List[str] = []
    env_touched_providers: List[str] = []
    env_created_models: List[str] = []
    env_touched_models: List[str] = []

    provider_fields: Dict[str, Dict[str, str]] = {}
    model_fields: Dict[str, str] = {}
    custom_fields: Dict[str, Dict[str, str]] = {}
    server_fields: Dict[str, Any] = {}
    common_fields: Dict[str, Any] = {}
    for key, value in env.items():
        match = ENV_PROVIDER_PATTERN.match(key)
        if match:
            provider_fields.setdefault(match.group(1), {})[match.group(2)] = value
            continue
        if key in ENV_MODEL_KEYS:
            model_fields[ENV_MODEL_KEYS[key]] = value
            continue
        match = ENV_CUSTOM_MODEL_PATTERN.match(key)
        if match:
            custom_fields.setdefault(match.group(1), {})[match.group(2)] = value
            continue
        if key in ENV_SERVER_KEYS:
            server_fields[ENV_SERVER_KEYS[key]] = value
            continue
        if key in ENV_COMMON_KEYS:
            common_fields[ENV_COMMON_KEYS[key]] = value
            continue
        if key.startswith(("VC_PROVIDER_", "VC_CUSTOM_MODEL_", "VC_MODEL_", "VC_SERVER_", "VC_COMMON_")):
            logger.warning("Unrecognized VC_* variable ignored: %s", key)
        # 其他 VC_* 变量不属于本次支持的覆盖范围，静默忽略

    for name_raw, fields in provider_fields.items():
        provider_key = _resolve_provider_key(providers, name_raw)
        target = providers.get(provider_key)
        created_here = not isinstance(target, dict)
        if created_here:
            target = {"protocol": "", "base_url": "", "api_key": "", "enable_proxy": False}
            providers[provider_key] = target
            env_created_providers.append(provider_key)
        for field, value in fields.items():
            field_name = field.lower()
            target[field_name] = _as_bool(value) if field_name == "enable_proxy" else value
            env_fields.append(f"api_providers.{provider_key}.{field_name}")
        if provider_key not in BUILTIN_PROVIDERS and not _is_custom_provider_complete(target):
            logger.warning("Env provider %s ignored: protocol/base_url incomplete", provider_key)
            if created_here:
                providers.pop(provider_key, None)
                env_created_providers.remove(provider_key)
            env_fields[:] = [
                path for path in env_fields if not path.startswith(f"api_providers.{provider_key}.")
            ]
            continue
        env_touched_providers.append(provider_key)

    models_section = effective.setdefault("models", {})
    for model_key, value in model_fields.items():
        models_section[model_key] = value
        env_fields.append(f"models.{model_key}")

    # API Server / Common 配置：支持 VC_SERVER__* / VC_COMMON__* 覆盖；
    # 未显式配置时，BACKEND_HOST/BACKEND_PORT（.env）作为回退，使本地直跑端口与 .env 对齐
    server_section = effective.setdefault("server", {})
    if "host" not in server_fields and env.get("BACKEND_HOST"):
        server_fields["host"] = env["BACKEND_HOST"]
    if "port" not in server_fields and env.get("BACKEND_PORT"):
        server_fields["port"] = env["BACKEND_PORT"]
    for field_name, raw in server_fields.items():
        if field_name == "port":
            try:
                server_value: Any = int(str(raw).strip())
            except (TypeError, ValueError):
                logger.warning("Env server.port ignored: invalid value")
                continue
        elif field_name == "access_log":
            server_value = _as_bool(raw)
        elif field_name == "log_level":
            server_value = _normalize_log_level(raw)
        else:
            server_value = "" if raw is None else str(raw)
        server_section[field_name] = server_value
        env_fields.append(f"server.{field_name}")
    if common_fields:
        common_section = providers.get("common")
        if not isinstance(common_section, dict):
            common_section = {}
            providers["common"] = common_section
        for field_name, raw in common_fields.items():
            if field_name == "print_model_input":
                common_section[field_name] = _as_bool(raw)
            else:
                common_section[field_name] = "" if raw is None else str(raw)
            env_fields.append(f"api_providers.common.{field_name}")

    custom_list = effective.setdefault("custom_models", [])
    custom_by_id = {
        entry.get("id"): entry
        for entry in custom_list
        if isinstance(entry, dict) and entry.get("id")
    }
    for group, fields in custom_fields.items():
        model_id = str(fields.get("ID") or "").strip()
        if not model_id:
            logger.warning("Env custom model group VC_CUSTOM_MODEL_%s ignored: missing ID", group)
            continue
        entry = custom_by_id.get(model_id)
        created_here = entry is None
        if created_here:
            entry = {"id": model_id, "provider": "", "model": "", "types": [], "abilities": []}
            custom_list.append(entry)
            custom_by_id[model_id] = entry
            env_created_models.append(model_id)
        for field, value in fields.items():
            field_name = field.lower()
            if field_name in ("types", "abilities"):
                entry[field_name] = _str_list(value)
            elif field_name == "concurrency":
                try:
                    entry["concurrency"] = int(value)
                except (TypeError, ValueError):
                    logger.warning("Env custom model %s: invalid concurrency ignored", model_id)
                    continue
            else:
                entry[field_name] = value
            env_fields.append(f"custom_models[{model_id}].{field_name}")
        provider_key = str(entry.get("provider") or "")
        if (
            not provider_key
            or provider_key not in providers
            or not entry.get("types")
            or (provider_key not in BUILTIN_PROVIDERS and not _is_custom_provider_complete(providers.get(provider_key)))
        ):
            logger.warning("Env custom model %s ignored: provider/types incomplete", model_id)
            custom_list.remove(entry)
            custom_by_id.pop(model_id, None)
            if model_id in env_created_models:
                env_created_models.remove(model_id)
            env_fields[:] = [
                path for path in env_fields if not path.startswith(f"custom_models[{model_id}].")
            ]
            continue
        env_touched_models.append(model_id)

    # env-only 判定：env 应用成功、但基线（文件视图）中不存在的条目 —— 保存时不写回文件
    baseline_view = baseline if isinstance(baseline, dict) else file_view
    baseline_providers = baseline_view.get("api_providers", {}) if isinstance(baseline_view, dict) else {}
    baseline_model_ids = {
        entry.get("id")
        for entry in (baseline_view.get("custom_models") or [])
        if isinstance(baseline_view, dict) and isinstance(entry, dict) and entry.get("id")
    }
    env_only_providers = [key for key in env_touched_providers if key not in (baseline_providers or {})]
    env_only_models = [mid for mid in env_touched_models if mid not in baseline_model_ids]
    env_info = {"fields": env_fields, "providers": env_only_providers, "models": env_only_models}
    return effective, env_info


def _strip_env_overridden(
    page_values: Dict[str, Any], env_info: Dict[str, Any], current_file: Dict[str, Any]
) -> Dict[str, Any]:
    """生成保存用文件视图：被 env 覆盖的字段恢复文件原值，env 引入的条目不写回文件。"""
    view = copy.deepcopy(page_values)
    providers = view.get("api_providers", {})
    models_section = view.get("models", {})
    custom_list = view.get("custom_models", [])
    file_providers = current_file.get("api_providers", {}) if isinstance(current_file, dict) else {}
    file_models = current_file.get("models", {}) if isinstance(current_file, dict) else {}
    file_custom = {
        entry.get("id"): entry
        for entry in (current_file.get("custom_models") or [])
        if isinstance(current_file, dict) and isinstance(entry, dict) and entry.get("id")
    }

    # 先剔除仅由 env 引入的模型条目，再做字段级恢复（避免逐字段恢复时触及这些条目）
    env_only_models = set(env_info.get("models", []))
    if env_only_models:
        custom_list = [entry for entry in custom_list if entry.get("id") not in env_only_models]
        view["custom_models"] = custom_list

    custom_pattern = re.compile(r"^custom_models\[([^\]]+)\]\.(\w+)$")
    for field_path in env_info.get("fields", []):
        if field_path.startswith("api_providers."):
            _, provider_key, field_name = field_path.split(".", 2)
            if provider_key in providers and isinstance(providers[provider_key], dict):
                file_value = _get(file_providers, f"{provider_key}.{field_name}")
                if file_value is None:
                    providers[provider_key].pop(field_name, None)
                else:
                    providers[provider_key][field_name] = copy.deepcopy(file_value)
        elif field_path.startswith("models."):
            model_key = field_path.split(".", 1)[1]
            file_value = _get(file_models, model_key)
            if file_value is None:
                models_section.pop(model_key, None)
            else:
                models_section[model_key] = copy.deepcopy(file_value)
        elif field_path.startswith("server."):
            # API Server 配置：恢复文件原值，不写入 .env 覆盖值
            field_name = field_path.split(".", 1)[1]
            file_server = current_file.get("server", {}) if isinstance(current_file, dict) else {}
            file_value = file_server.get(field_name) if isinstance(file_server, dict) else None
            server_view = view.setdefault("server", {})
            if file_value is None:
                server_view.pop(field_name, None)
            else:
                server_view[field_name] = copy.deepcopy(file_value)
        else:
            match = custom_pattern.match(field_path)
            if not match:
                continue
            model_id, field_name = match.group(1), match.group(2)
            if field_name == "id":
                # id 是条目身份标识，不作为可恢复字段处理
                continue
            target = next((entry for entry in custom_list if entry.get("id") == model_id), None)
            if target is None:
                continue
            file_entry = file_custom.get(model_id)
            if isinstance(file_entry, dict) and field_name in file_entry:
                target[field_name] = copy.deepcopy(file_entry[field_name])
            else:
                target.pop(field_name, None)

    for provider_key in env_info.get("providers", []):
        providers.pop(provider_key, None)
    return view


def _validate_effective_config(config: Dict[str, Any]) -> None:
    """保存路径硬校验；不通过时抛出 ValueError（消息面向用户，逐条列出原因）。"""
    providers = config.get("api_providers", {}) or {}
    builtin_ids: set = set()
    try:
        from models.config_model import MODEL_CONFIG  # 惰性导入，避免模块循环依赖
        builtin_ids = set(MODEL_CONFIG.get("models", {}).keys())
    except Exception:  # pragma: no cover - 注册表不可用时仅跳过内置 id 冲突校验
        logger.warning("Builtin model registry unavailable for validation", exc_info=True)

    errors: List[str] = []
    for key, provider_config in providers.items():
        if key == "common" or key in BUILTIN_PROVIDERS:
            continue
        if not isinstance(provider_config, dict):
            errors.append(f"供应商 {key} 配置必须是对象")
            continue
        if provider_config.get("protocol") not in CUSTOM_PROTOCOLS:
            errors.append(f"供应商 {key} 的 protocol 非法或缺失（可选：{'/'.join(CUSTOM_PROTOCOLS)}）")
        base_url = str(provider_config.get("base_url") or "")
        if not base_url.startswith(("http://", "https://")):
            errors.append(f"供应商 {key} 的 base_url 必须为 http(s) 地址")

    seen_ids: set = set()
    for index, entry in enumerate(config.get("custom_models", []) or [], start=1):
        if not isinstance(entry, dict):
            errors.append(f"custom_models 第 {index} 条必须是对象")
            continue
        model_id = str(entry.get("id") or "").strip()
        if not model_id:
            errors.append(f"custom_models 第 {index} 条缺少 id")
            continue
        if model_id in seen_ids:
            errors.append(f"自定义模型 id 重复：{model_id}")
        if model_id in builtin_ids:
            errors.append(f"自定义模型 id 与内置模型冲突：{model_id}")
        seen_ids.add(model_id)
        provider_key = str(entry.get("provider") or "")
        if not provider_key:
            errors.append(f"自定义模型 {model_id} 缺少 provider")
        elif provider_key in BUILTIN_PROVIDERS:
            errors.append(
                f"自定义模型 {model_id} 的 provider 必须是自定义供应商（内置供应商不支持注册自定义模型）：{provider_key}"
            )
        elif provider_key not in providers:
            errors.append(f"自定义模型 {model_id} 引用的供应商不存在：{provider_key}")
        if not entry.get("types"):
            errors.append(f"自定义模型 {model_id} 的 types 不能为空")
    if errors:
        raise ValueError("；".join(errors))


def _sanitize_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """加载路径容错：丢弃引用缺失/不完整供应商或字段缺失的自定义模型条目。"""
    providers = config.get("api_providers", {}) or {}
    valid: List[Dict[str, Any]] = []
    for entry in config.get("custom_models", []) or []:
        if not isinstance(entry, dict):
            logger.warning("custom_models entry ignored at load: not a mapping")
            continue
        model_id = str(entry.get("id") or "").strip()
        provider_key = str(entry.get("provider") or "")
        provider_config = providers.get(provider_key)
        if (
            not model_id
            or not provider_key
            or provider_key in BUILTIN_PROVIDERS
            or not isinstance(provider_config, dict)
            or not entry.get("types")
            or not _is_custom_provider_complete(provider_config)
        ):
            logger.warning(
                "custom_models entry ignored at load: id=%s provider=%s",
                model_id or "<missing>",
                provider_key or "<missing>",
            )
            continue
        valid.append(entry)
    clean = copy.deepcopy(config)
    clean["custom_models"] = valid
    return clean


def load_config() -> Dict[str, Any]:
    """加载 config.yaml（文件视图，不含环境变量覆盖层）。"""
    if not CONFIG_PATH.exists():
        source = CONFIG_EXAMPLE_PATH if CONFIG_EXAMPLE_PATH.exists() else None
        if source:
            with source.open("r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            return _coerce_config(loaded)
        return copy.deepcopy(DEFAULT_CONFIG)

    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    if not isinstance(loaded, dict):
        raise ValueError("backend/config.yaml must contain a YAML mapping.")
    return _coerce_config(loaded)


def _write_config_file(values: Dict[str, Any]) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(values, f, allow_unicode=True, sort_keys=False)


def _load_raw_provider_keys() -> set:
    """读取配置文件中显式声明的 api_providers 键（未经默认合并）。

    用于「设置页保存不回写内置供应商」：文件里没有的内置键（如已注释/删除的
    openai / dashscope / ...）保存后仍不写回，保持文件结构稳定。
    """
    if not CONFIG_PATH.exists():
        return set()
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        providers = loaded.get("api_providers", {})
        return set(providers.keys()) if isinstance(providers, dict) else set()
    except Exception:  # noqa: BLE001 - 读取失败时按无任何键处理
        return set()


def _prune_absent_builtin_providers(view: Dict[str, Any], raw_keys: set) -> None:
    """剔除视图中「文件未显式声明」的内置供应商（保留 common），避免保存写回注释/删除过的内置配置。"""
    providers_view = view.get("api_providers")
    if not isinstance(providers_view, dict):
        return
    for key in BUILTIN_PROVIDERS:
        if key not in raw_keys:
            providers_view.pop(key, None)


def save_config(values: Dict[str, Any]) -> Dict[str, Any]:
    """兼容入口：归一化后写入配置文件（不应用环境变量层）。"""
    clean = _coerce_config(values)
    _write_config_file(clean)
    return clean


# 文件视图（不含 env）作为保存时“恢复被覆盖字段原值”的来源；CONFIG 为最终有效配置（文件 + env）
_FILE_CONFIG_VALUES: Dict[str, Any] = _sanitize_config(load_config())
# 配置文件中显式声明的供应商键（用于保存时裁剪“被注释/删除”的内置供应商）
_FILE_RAW_PROVIDER_KEYS: set = _load_raw_provider_keys()
CONFIG_VALUES, CONFIG_ENV_INFO = _apply_env_overrides(_FILE_CONFIG_VALUES)


class Config:
    CONFIG = CONFIG_VALUES
    ENV_OVERRIDES = CONFIG_ENV_INFO

    HOST = _get(CONFIG, "server.host")
    PORT = _get(CONFIG, "server.port")
    LOG_LEVEL = _get(CONFIG, "server.log_level")
    DEBUG = LOG_LEVEL == "DEBUG"
    ACCESS_LOG = _get(CONFIG, "server.access_log")

    PRINT_MODEL_INPUT = _get(CONFIG, "api_providers.common.print_model_input")
    PROXY = _get(CONFIG, "api_providers.common.proxy")

    OPENAI_API_KEY = _get(CONFIG, "api_providers.openai.api_key")
    OPENAI_BASE_URL = _get(CONFIG, "api_providers.openai.base_url")
    OPENAI_ENABLE_PROXY = _get(CONFIG, "api_providers.openai.enable_proxy")
    GEMINI_API_KEY = _get(CONFIG, "api_providers.gemini.api_key")
    GOOGLE_GEMINI_BASE_URL = _get(CONFIG, "api_providers.gemini.base_url")
    GEMINI_ENABLE_PROXY = _get(CONFIG, "api_providers.gemini.enable_proxy")
    DEEPSEEK_API_KEY = _get(CONFIG, "api_providers.deepseek.api_key")
    DEEPSEEK_BASE_URL = _get(CONFIG, "api_providers.deepseek.base_url")
    DEEPSEEK_ENABLE_PROXY = _get(CONFIG, "api_providers.deepseek.enable_proxy")
    DASHSCOPE_API_KEY = _get(CONFIG, "api_providers.dashscope.api_key")
    DASHSCOPE_BASE_URL = _get(CONFIG, "api_providers.dashscope.base_url")
    DASHSCOPE_ENABLE_PROXY = _get(CONFIG, "api_providers.dashscope.enable_proxy")
    ARK_API_KEY = _get(CONFIG, "api_providers.ark.api_key")
    ARK_BASE_URL = _get(CONFIG, "api_providers.ark.base_url")
    ARK_ENABLE_PROXY = _get(CONFIG, "api_providers.ark.enable_proxy")
    KLING_API_KEY = _get(CONFIG, "api_providers.kling.api_key")
    KLING_BASE_URL = _get(CONFIG, "api_providers.kling.base_url")
    KLING_ENABLE_PROXY = _get(CONFIG, "api_providers.kling.enable_proxy")

    LLM_API_KEY = DASHSCOPE_API_KEY
    LLM_BASE_URL = ""
    LLM_MODEL = _get(CONFIG, "models.llm")
    VLM_MODEL = _get(CONFIG, "models.vlm")
    IMAGE_IT2I_MODEL = _get(CONFIG, "models.image_it2i")
    IMAGE_T2I_MODEL = _get(CONFIG, "models.image_t2i")
    VIDEO_MODEL = _get(CONFIG, "models.video")
    VIDEO_FIRST_FRAME_MODEL = _get(CONFIG, "models.video_first_frame")
    VIDEO_START_END_MODEL = _get(CONFIG, "models.video_start_end")
    VIDEO_REFERENCE_MODEL = _get(CONFIG, "models.video_reference")
    VIDEO_RATIO = _get(CONFIG, "generation.video_ratio")
    VIDEO_RESOLUTION = _get(CONFIG, "generation.video_resolution")
    VIDEO_GENERATION_MODE = _get(CONFIG, "generation.video_generation_mode")
    STYLE = _get(CONFIG, "generation.style")

    BASE_DIR = str(BASE_DIR)
    CODE_DIR = os.path.join(BASE_DIR, "code")
    RESULT_DIR = os.path.join(CODE_DIR, "result")
    TEMP_DIR = os.path.join(BASE_DIR, "temp")
    SESSION_DIR = os.path.join(CODE_DIR, "data", "sessions")
    TASK_DIR = os.path.join(CODE_DIR, "data", "tasks")
    TASK_RESULT_DIR = os.path.join(RESULT_DIR, "task")

    @classmethod
    def as_dict(cls) -> Dict[str, Any]:
        return copy.deepcopy(cls.CONFIG)

    @classmethod
    def provider_proxy(cls, provider: str) -> str:
        provider_config = _get(cls.CONFIG, f"api_providers.{provider}", {})
        if not isinstance(provider_config, dict) or not _as_bool(provider_config.get("enable_proxy")):
            return ""
        return cls.PROXY or ""

    @classmethod
    def requests_proxies(cls, provider: str) -> Optional[Dict[str, str]]:
        proxy = cls.provider_proxy(provider)
        if not proxy:
            return None
        return {"http": proxy, "https": proxy}

    @classmethod
    def update_config(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        global _FILE_CONFIG_VALUES, _FILE_RAW_PROVIDER_KEYS

        coerced = _coerce_config(values)
        effective, env_info = _apply_env_overrides(coerced, baseline=_FILE_CONFIG_VALUES)
        # 硬校验不通过时抛出 ValueError，由 API 层转换为 400（不落盘、不改内存状态）
        _validate_effective_config(effective)
        # 文件视图：被 env 覆盖的字段恢复文件原值，env 引入的条目不写回文件
        file_view = _strip_env_overridden(coerced, env_info, _FILE_CONFIG_VALUES)
        # 不回写文件未声明的内置供应商（保护配置文件里“已注释/删除”的结构）
        _prune_absent_builtin_providers(file_view, _FILE_RAW_PROVIDER_KEYS)
        _write_config_file(file_view)
        _FILE_CONFIG_VALUES = file_view
        _FILE_RAW_PROVIDER_KEYS = set((file_view.get("api_providers") or {}).keys())
        cls.CONFIG = effective
        cls.ENV_OVERRIDES = env_info

        clean = effective

        cls.HOST = _get(clean, "server.host")
        cls.PORT = _get(clean, "server.port")
        cls.LOG_LEVEL = _get(clean, "server.log_level")
        cls.DEBUG = cls.LOG_LEVEL == "DEBUG"
        cls.ACCESS_LOG = _get(clean, "server.access_log")

        cls.PRINT_MODEL_INPUT = _get(clean, "api_providers.common.print_model_input")
        cls.PROXY = _get(clean, "api_providers.common.proxy")

        cls.OPENAI_API_KEY = _get(clean, "api_providers.openai.api_key")
        cls.OPENAI_BASE_URL = _get(clean, "api_providers.openai.base_url")
        cls.OPENAI_ENABLE_PROXY = _get(clean, "api_providers.openai.enable_proxy")
        cls.GEMINI_API_KEY = _get(clean, "api_providers.gemini.api_key")
        cls.GOOGLE_GEMINI_BASE_URL = _get(clean, "api_providers.gemini.base_url")
        cls.GEMINI_ENABLE_PROXY = _get(clean, "api_providers.gemini.enable_proxy")
        cls.DEEPSEEK_API_KEY = _get(clean, "api_providers.deepseek.api_key")
        cls.DEEPSEEK_BASE_URL = _get(clean, "api_providers.deepseek.base_url")
        cls.DEEPSEEK_ENABLE_PROXY = _get(clean, "api_providers.deepseek.enable_proxy")
        cls.DASHSCOPE_API_KEY = _get(clean, "api_providers.dashscope.api_key")
        cls.DASHSCOPE_BASE_URL = _get(clean, "api_providers.dashscope.base_url")
        cls.DASHSCOPE_ENABLE_PROXY = _get(clean, "api_providers.dashscope.enable_proxy")
        cls.ARK_API_KEY = _get(clean, "api_providers.ark.api_key")
        cls.ARK_BASE_URL = _get(clean, "api_providers.ark.base_url")
        cls.ARK_ENABLE_PROXY = _get(clean, "api_providers.ark.enable_proxy")
        cls.KLING_API_KEY = _get(clean, "api_providers.kling.api_key")
        cls.KLING_BASE_URL = _get(clean, "api_providers.kling.base_url")
        cls.KLING_ENABLE_PROXY = _get(clean, "api_providers.kling.enable_proxy")

        cls.LLM_API_KEY = cls.DASHSCOPE_API_KEY
        cls.LLM_BASE_URL = ""
        cls.LLM_MODEL = _get(clean, "models.llm")
        cls.VLM_MODEL = _get(clean, "models.vlm")
        cls.IMAGE_IT2I_MODEL = _get(clean, "models.image_it2i")
        cls.IMAGE_T2I_MODEL = _get(clean, "models.image_t2i")
        cls.VIDEO_MODEL = _get(clean, "models.video")
        cls.VIDEO_FIRST_FRAME_MODEL = _get(clean, "models.video_first_frame")
        cls.VIDEO_START_END_MODEL = _get(clean, "models.video_start_end")
        cls.VIDEO_REFERENCE_MODEL = _get(clean, "models.video_reference")
        cls.VIDEO_RATIO = _get(clean, "generation.video_ratio")
        cls.VIDEO_RESOLUTION = _get(clean, "generation.video_resolution")
        cls.VIDEO_GENERATION_MODE = _get(clean, "generation.video_generation_mode")
        cls.STYLE = _get(clean, "generation.style")
        return cls.as_dict()

    @classmethod
    def check_dirs(cls):
        data_dir = os.path.join(cls.CODE_DIR, "data")
        for directory in [
            cls.CODE_DIR,
            data_dir,
            cls.SESSION_DIR,
            cls.TASK_DIR,
            cls.RESULT_DIR,
            cls.TASK_RESULT_DIR,
            cls.TEMP_DIR,
        ]:
            if not os.path.exists(directory):
                os.makedirs(directory, exist_ok=True)
                logger.info("Created directory: %s", directory)


Config.check_dirs()
settings = Config()
