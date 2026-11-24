"""视频资源池运行时 YAML：加载、归一、引用校验与原子写。

本模块是纯配置层：不读写数据库、不访问模型服务、不导入调度器。运行时的
``video_profiles.yaml`` 只保存池/实例/策略；全局开关与文件路径来自 ``.env``，
两者由调用方（config.py / API）读取后传入，避免出现第二个来源。

YAML 顶层固定为 ``video_dispatch`` / ``video_profiles`` / ``video_instances``。
``video_dispatch.enabled`` 明确禁止：开关只允许来自 ``VC_VIDEO_DISPATCH__ENABLED``，
否则会出现“env 已关闭、YAML 仍写 true”的双来源。
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import yaml

DISPATCH_MODES = ("first_frame", "start_end_frame", "reference")
CONTRACT_MODES = DISPATCH_MODES
TASKS = ("t2va", "fl2va", "ref2va")
ACCESS_MODES = ("exclusive", "shared")
PROBE_KINDS = ("vllm_omni", "sglang_diffusion", "custom")

DEFAULT_MODE = "auto"
DEFAULT_QUEUE_TIMEOUT_SECONDS = 3600
DEFAULT_MAX_QUEUED_JOBS = 1000
DEFAULT_MAX_RUNNING_JOBS = 8
DEFAULT_MAX_CONCURRENCY_PER_EXECUTION = 8

_DISPATCH_KEYS = {
    "default_mode",
    "default_profiles",
    "queue_timeout_seconds",
    "max_queued_jobs",
    "max_running_jobs",
}
_PROFILE_KEYS = {
    "id",
    "name",
    "revision",
    "model_family",
    "artifact_revision",
    "prompt_contract_revision",
    "sampling_contract_revision",
    "instances",
    "contracts",
    "enabled",
    "max_concurrency_per_execution",
}
_INSTANCE_KEYS = {
    "instance_id",
    "model_id",
    "resource_key",
    "access_mode",
    "enabled",
    "driver",
    "probe",
}
_PROBE_KEYS = {"kind", "jobs_url", "metrics_url", "labels"}
_CONTRACT_KEYS = {"task", "resolutions", "short_edge", "ratios", "duration", "fps"}

PLACEHOLDER_TEXT = "CHANGE_ME"
_SAMPLE_RESOURCE_RE = re.compile(r"^example-gpu-\d+$", re.IGNORECASE)


class VideoPoolConfigError(ValueError):
    """运行时池配置不合法；消息直接面向管理员。"""


class PoolConfigConflict(VideoPoolConfigError):
    """保存时内容修订已变化。"""


@dataclass(frozen=True)
class VideoPoolSnapshot:
    data: Dict[str, Any]
    revision: str


# 最近一次成功加载/保存的有效快照（进程内）。文件损坏时，受管 guard 用它判断
# 哪些模型已经纳管：旧快照中不存在的“新受管模型”会被当作非受管放行，这是
# 最后有效快照策略的已知限制；通过保存接口写入的新模型会先进入本缓存。
_LAST_GOOD_SNAPSHOTS: Dict[str, VideoPoolSnapshot] = {}


def last_good_snapshot(path: Path) -> Optional[VideoPoolSnapshot]:
    """返回该路径最近一次成功加载/保存的快照；从未成功过时返回 None。"""
    return _LAST_GOOD_SNAPSHOTS.get(str(Path(path)))


def _remember_snapshot(
    path: Path, snapshot: VideoPoolSnapshot, *, explicit_save: bool = False
) -> None:
    """记录最后有效快照。

    普通加载得到的空实例列表可能来自文件被瞬时截断/清空，不能覆盖已有
    非空缓存，否则一次截断就会清空“已知受管模型”集合；只有管理员通过
    保存接口显式写入时，才允许用空池替换缓存（明确的清空语义）。

    已知限制：普通加载得到的“语法合法但只保留部分实例”的文件仍会被当作
    新快照，可能缩小受管集合；需要严格保证时应通过显式保存/宿主重载刷新，
    并审计宿主手工编辑。
    """
    key = str(Path(path))
    previous = _LAST_GOOD_SNAPSHOTS.get(key)
    if (
        not explicit_save
        and previous is not None
        and previous.data.get("video_instances")
        and not snapshot.data.get("video_instances")
    ):
        return
    _LAST_GOOD_SNAPSHOTS[key] = snapshot


def default_pool_config() -> Dict[str, Any]:
    """首次启动/文件缺失时的空池默认；不得包含任何推荐池 ID。"""
    return {
        "video_dispatch": {
            "default_mode": DEFAULT_MODE,
            "default_profiles": {},
            "queue_timeout_seconds": DEFAULT_QUEUE_TIMEOUT_SECONDS,
            "max_queued_jobs": DEFAULT_MAX_QUEUED_JOBS,
            "max_running_jobs": DEFAULT_MAX_RUNNING_JOBS,
        },
        "video_profiles": [],
        "video_instances": [],
    }


def resolve_dispatch_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """读取 ``VC_VIDEO_DISPATCH__ENABLED``；缺省/空值视为 true。"""
    source = os.environ if env is None else env
    raw = source.get("VC_VIDEO_DISPATCH__ENABLED")
    if raw is None or str(raw).strip() == "":
        return True
    return _parse_bool(raw, "VC_VIDEO_DISPATCH__ENABLED")


def resolve_profiles_path(
    env: Optional[Mapping[str, str]] = None,
    *,
    config_path: Optional[Path] = None,
) -> Path:
    """读取 ``VC_VIDEO_PROFILES_FILE``；未设置时默认 ``CONFIG_PATH.parent/data/video_profiles.yaml``。"""
    source = os.environ if env is None else env
    raw = str(source.get("VC_VIDEO_PROFILES_FILE") or "").strip()
    if raw:
        return Path(raw).expanduser()
    if config_path is None:
        from config import CONFIG_PATH  # 延迟导入，避免纯配置模块拉起重依赖

        config_path = CONFIG_PATH
    return Path(config_path).parent / "data" / "video_profiles.yaml"


def compute_revision(data: Mapping[str, Any]) -> str:
    """内容修订：规范化后的 YAML 文本摘要，手工编辑也能比对。"""
    canonical = yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def load_pool_config(
    path: Path,
    *,
    providers: Optional[Mapping[str, Any]] = None,
    models: Optional[Mapping[str, Any]] = None,
) -> VideoPoolSnapshot:
    """读取并校验运行时 YAML；文件缺失/空文件返回空池默认。"""
    file_path = Path(path)
    if not file_path.exists():
        data = default_pool_config()
        return VideoPoolSnapshot(data=data, revision=compute_revision(data))
    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise VideoPoolConfigError(f"无法读取池配置文件 {file_path}：{exc}") from exc
    except yaml.YAMLError as exc:
        raise VideoPoolConfigError(f"池配置文件不是合法 YAML：{exc}") from exc
    if raw is None:
        raw = {}
    data = normalize_pool_config(raw)
    validate_pool_config(data, providers=providers, models=models)
    snapshot = VideoPoolSnapshot(data=data, revision=compute_revision(data))
    _remember_snapshot(file_path, snapshot)
    return snapshot


def normalize_pool_config(raw: Any) -> Dict[str, Any]:
    """归一化运行时 YAML；缺省键补默认值，未知键/错误类型直接拒绝。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise VideoPoolConfigError("池配置顶层必须是映射")
    _reject_unknown_keys(raw, {"video_dispatch", "video_profiles", "video_instances"}, "顶层")

    dispatch_raw = raw.get("video_dispatch")
    if dispatch_raw is None:
        dispatch_raw = {}
    if not isinstance(dispatch_raw, dict):
        raise VideoPoolConfigError("video_dispatch 必须是映射")
    if "enabled" in dispatch_raw:
        raise VideoPoolConfigError(
            "video_dispatch.enabled 不允许写在运行时 YAML；开关只来自 .env 的 "
            "VC_VIDEO_DISPATCH__ENABLED"
        )
    _reject_unknown_keys(dispatch_raw, _DISPATCH_KEYS, "video_dispatch")

    default_mode = str(dispatch_raw.get("default_mode") or DEFAULT_MODE).strip().lower()
    if default_mode != "auto":
        raise VideoPoolConfigError("video_dispatch.default_mode 只支持 auto")

    default_profiles = dispatch_raw.get("default_profiles")
    if default_profiles is None:
        default_profiles = {}
    if not isinstance(default_profiles, dict):
        raise VideoPoolConfigError("video_dispatch.default_profiles 必须是映射")
    normalized_defaults: Dict[str, str] = {}
    for mode, pool_id in default_profiles.items():
        mode_key = str(mode).strip()
        if mode_key not in DISPATCH_MODES:
            raise VideoPoolConfigError(f"default_profiles 含未知生成方式：{mode_key}")
        if not isinstance(pool_id, str) or not pool_id.strip():
            raise VideoPoolConfigError(f"default_profiles.{mode_key} 必须是非空池 ID")
        normalized_defaults[mode_key] = pool_id.strip()

    profiles = _normalize_profiles(raw.get("video_profiles"))
    instances = _normalize_instances(raw.get("video_instances"))

    return {
        "video_dispatch": {
            "default_mode": default_mode,
            "default_profiles": normalized_defaults,
            "queue_timeout_seconds": _parse_int(
                dispatch_raw.get("queue_timeout_seconds", DEFAULT_QUEUE_TIMEOUT_SECONDS),
                "video_dispatch.queue_timeout_seconds",
                minimum=1,
            ),
            "max_queued_jobs": _parse_int(
                dispatch_raw.get("max_queued_jobs", DEFAULT_MAX_QUEUED_JOBS),
                "video_dispatch.max_queued_jobs",
                minimum=1,
            ),
            "max_running_jobs": _parse_int(
                dispatch_raw.get("max_running_jobs", DEFAULT_MAX_RUNNING_JOBS),
                "video_dispatch.max_running_jobs",
                minimum=1,
                maximum=8,
            ),
        },
        "video_profiles": profiles,
        "video_instances": instances,
    }


def validate_pool_config(
    data: Mapping[str, Any],
    *,
    providers: Optional[Mapping[str, Any]] = None,
    models: Optional[Mapping[str, Any]] = None,
) -> None:
    """校验唯一 ID、成员/模型/供应商引用与默认池映射；不做资格/探针判定。"""
    profiles = data.get("video_profiles")
    instances = data.get("video_instances")
    if not isinstance(profiles, list) or not isinstance(instances, list):
        raise VideoPoolConfigError("video_profiles / video_instances 必须是列表")

    profile_by_id = _index_unique(profiles, "video_profiles", "id")
    instance_by_id = _index_unique(instances, "video_instances", "instance_id")
    _index_unique(instances, "video_instances", "resource_key", allow_duplicates=True)

    for profile in profiles:
        for instance_id in profile["instances"]:
            if instance_id not in instance_by_id:
                raise VideoPoolConfigError(
                    f"池 {profile['id']} 引用了不存在的实例：{instance_id}"
                )

    if models is not None:
        for instance in instances:
            model_id = instance["model_id"]
            meta = models.get(model_id)
            if not isinstance(meta, dict):
                raise VideoPoolConfigError(
                    f"实例 {instance['instance_id']} 引用了未注册模型：{model_id}"
                )
            if providers is not None:
                provider_key = str(meta.get("provider") or "")
                if provider_key not in providers:
                    raise VideoPoolConfigError(
                        f"实例 {instance['instance_id']} 的模型 {model_id} 引用了未注册供应商："
                        f"{provider_key or '<empty>'}"
                    )

    defaults = data["video_dispatch"]["default_profiles"]
    for mode, pool_id in defaults.items():
        profile = profile_by_id.get(pool_id)
        if profile is None:
            raise VideoPoolConfigError(
                f"default_profiles.{mode} 引用了不存在的池：{pool_id}"
            )
        if mode not in profile["contracts"]:
            raise VideoPoolConfigError(
                f"default_profiles.{mode} 指向的池 {pool_id} 未声明该生成方式契约"
            )


def collect_placeholder_issues(
    data: Mapping[str, Any],
    *,
    providers: Optional[Mapping[str, Any]] = None,
    models: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, str]]:
    """返回字段级占位/待填清单；不扫描注释、显示名或无关文本。"""
    issues: list[dict[str, str]] = []
    for profile in data.get("video_profiles") or []:
        issues.extend(_profile_placeholder_issues(profile))
    for instance in data.get("video_instances") or []:
        issues.extend(_instance_placeholder_issues(instance, providers, models))
    return issues


def validate_enabled_instances(
    data: Mapping[str, Any],
    *,
    providers: Optional[Mapping[str, Any]] = None,
    models: Optional[Mapping[str, Any]] = None,
) -> None:
    """启用中的实例若仍有占位字段则拒绝保存/生效；禁用草稿不在此拦截。"""
    for instance in data.get("video_instances") or []:
        if not instance.get("enabled"):
            continue
        issues = _instance_placeholder_issues(instance, providers, models)
        if issues:
            details = "; ".join(f"{item['field']}: {item['reason']}" for item in issues)
            raise VideoPoolConfigError(
                f"实例 {instance['instance_id']} 启用前仍有占位配置：{details}"
            )


def effective_instance_view(
    data: Mapping[str, Any],
    *,
    providers: Optional[Mapping[str, Any]] = None,
    models: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """实例的有效启用视图：enabled=true 但存在占位字段时 effective_enabled=false。"""
    result: list[dict[str, Any]] = []
    for instance in data.get("video_instances") or []:
        issues = _instance_placeholder_issues(instance, providers, models)
        result.append(
            {
                "instance_id": instance["instance_id"],
                "enabled": bool(instance.get("enabled")),
                "effective_enabled": bool(instance.get("enabled")) and not issues,
                "issues": issues,
            }
        )
    return result


def effective_pool_view(
    data: Mapping[str, Any],
    *,
    providers: Optional[Mapping[str, Any]] = None,
    models: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """池的有效启用视图：profile.enabled 且至少一个成员有效启用。"""
    instance_views = {
        item["instance_id"]: item
        for item in effective_instance_view(data, providers=providers, models=models)
    }
    result: list[dict[str, Any]] = []
    for profile in data.get("video_profiles") or []:
        members = [instance_views.get(member) for member in profile.get("instances") or []]
        members = [item for item in members if item is not None]
        issues = _profile_placeholder_issues(profile)
        result.append(
            {
                "profile_id": profile["id"],
                "enabled": bool(profile.get("enabled", True)),
                "effective_enabled": bool(profile.get("enabled", True))
                and any(item["effective_enabled"] for item in members),
                "instances": members,
                "issues": issues,
            }
        )
    return result


def write_pool_config(
    path: Path,
    raw: Any,
    *,
    expected_revision: Optional[str] = None,
    providers: Optional[Mapping[str, Any]] = None,
    models: Optional[Mapping[str, Any]] = None,
    enforce_enabled: bool = True,
) -> VideoPoolSnapshot:
    """校验并原子写入运行时 YAML；旧修订返回 PoolConfigConflict。"""
    data = normalize_pool_config(raw)
    validate_pool_config(data, providers=providers, models=models)
    if enforce_enabled:
        validate_enabled_instances(data, providers=providers, models=models)

    file_path = Path(path)
    if expected_revision is not None:
        current = load_pool_config(file_path, providers=providers, models=models).revision
        if str(expected_revision) != current:
            raise PoolConfigConflict(
                "池配置已被其他操作修改，请刷新后重试"
            )

    if not file_path.parent.exists():
        raise VideoPoolConfigError(f"池配置目录不存在：{file_path.parent}")
    payload = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    tmp_path: Optional[Path] = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(file_path.parent),
            prefix=f".{file_path.name}.",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, file_path)
    except OSError as exc:
        raise VideoPoolConfigError(f"池配置写入失败：{exc}") from exc
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    snapshot = VideoPoolSnapshot(data=data, revision=compute_revision(data))
    _remember_snapshot(file_path, snapshot, explicit_save=True)
    return snapshot


class VideoPoolConfigStore:
    """单进程内的池配置快照；按 mtime + 摘要检测宿主手工编辑。"""

    def __init__(
        self,
        path: Path,
        *,
        providers: Optional[Mapping[str, Any]] = None,
        models: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.path = Path(path)
        self._providers = providers
        self._models = models
        self._snapshot: Optional[VideoPoolSnapshot] = None
        self._mtime_ns: Optional[int] = None

    @property
    def snapshot(self) -> VideoPoolSnapshot:
        if self._snapshot is None:
            return self.load()
        return self._snapshot

    def load(self) -> VideoPoolSnapshot:
        snapshot = load_pool_config(
            self.path,
            providers=self._providers,
            models=self._models,
        )
        self._snapshot = snapshot
        self._mtime_ns = _mtime_ns(self.path)
        return snapshot

    def reload_if_changed(self) -> Tuple[VideoPoolSnapshot, bool]:
        """文件 mtime 变化时重载；解析失败抛错并保留上一份有效快照。"""
        mtime = _mtime_ns(self.path)
        if self._snapshot is not None and mtime == self._mtime_ns:
            return self._snapshot, False
        return self.load(), True

    def save(self, raw: Any, *, expected_revision: Optional[str] = None) -> VideoPoolSnapshot:
        snapshot = write_pool_config(
            self.path,
            raw,
            expected_revision=expected_revision,
            providers=self._providers,
            models=self._models,
        )
        self._snapshot = snapshot
        self._mtime_ns = _mtime_ns(self.path)
        return snapshot


def is_placeholder_text(value: Any) -> bool:
    """完整值（去空白、忽略大小写）等于 CHANGE_ME 才算占位。"""
    return str(value or "").strip().upper() == PLACEHOLDER_TEXT


def _is_example_host(url: Any) -> bool:
    """hostname 规范化后等于 example 或以 .example 结尾；不按子串匹配。"""
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    normalized = host.lower().rstrip(".")
    return normalized == "example" or normalized.endswith(".example")


def _issue(field: str, code: str, reason: str) -> dict[str, str]:
    return {"field": field, "code": code, "reason": reason}


def _profile_placeholder_issues(profile: Mapping[str, Any]) -> list[dict[str, str]]:
    profile_id = str(profile.get("id") or "")
    issues: list[dict[str, str]] = []
    for key in (
        "artifact_revision",
        "prompt_contract_revision",
        "sampling_contract_revision",
    ):
        if is_placeholder_text(profile.get(key)):
            issues.append(
                _issue(
                    f"video_profiles.{profile_id}.{key}",
                    "change_me",
                    "契约修订仍为 CHANGE_ME，池不视为就绪",
                )
            )
    return issues


def _instance_placeholder_issues(
    instance: Mapping[str, Any],
    providers: Optional[Mapping[str, Any]],
    models: Optional[Mapping[str, Any]],
) -> list[dict[str, str]]:
    instance_id = str(instance.get("instance_id") or "")
    issues: list[dict[str, str]] = []
    probe = instance.get("probe") if isinstance(instance.get("probe"), dict) else {}
    for key in ("jobs_url", "metrics_url"):
        value = probe.get(key)
        if value and _is_example_host(value):
            issues.append(
                _issue(
                    f"video_instances.{instance_id}.probe.{key}",
                    "example_host",
                    "仍使用 example 保留域名，不能启用或自动探测",
                )
            )
    if _SAMPLE_RESOURCE_RE.match(str(instance.get("resource_key") or "").strip()):
        issues.append(
            _issue(
                f"video_instances.{instance_id}.resource_key",
                "sample_resource_key",
                "仍为 example-gpu-数字 样例资源键，需填写真实物理资源",
            )
        )
    labels = probe.get("labels") if isinstance(probe.get("labels"), dict) else {}
    for key, value in labels.items():
        if is_placeholder_text(value):
            issues.append(
                _issue(
                    f"video_instances.{instance_id}.probe.labels.{key}",
                    "change_me",
                    "探针标签仍为 CHANGE_ME，需按真实 role/replica 填写",
                )
            )
    if models is not None:
        meta = models.get(instance.get("model_id"))
        if isinstance(meta, dict):
            if is_placeholder_text(meta.get("model")):
                issues.append(
                    _issue(
                        f"custom_models.{instance.get('model_id')}.model",
                        "change_me",
                        "服务端 model 仍为 CHANGE_ME",
                    )
                )
            if providers is not None:
                provider_key = str(meta.get("provider") or "")
                provider = providers.get(provider_key)
                if isinstance(provider, dict) and _is_example_host(provider.get("base_url")):
                    issues.append(
                        _issue(
                            f"api_providers.{provider_key}.base_url",
                            "example_host",
                            "供应商地址仍使用 example 保留域名",
                        )
                    )
    return issues


def _normalize_profiles(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise VideoPoolConfigError("video_profiles 必须是列表")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        where = f"video_profiles[{index}]"
        if not isinstance(item, dict):
            raise VideoPoolConfigError(f"{where} 必须是映射")
        _reject_unknown_keys(item, _PROFILE_KEYS, where)
        profile_id = _require_str(item.get("id"), f"{where}.id")
        instances = item.get("instances")
        if not isinstance(instances, list) or not instances:
            raise VideoPoolConfigError(f"{where}.instances 必须是非空列表")
        normalized.append(
            {
                "id": profile_id,
                "name": str(item.get("name") or profile_id),
                "revision": _parse_int(item.get("revision", 1), f"{where}.revision", minimum=1),
                "model_family": str(item.get("model_family") or ""),
                "artifact_revision": str(item.get("artifact_revision") or ""),
                "prompt_contract_revision": str(item.get("prompt_contract_revision") or ""),
                "sampling_contract_revision": str(item.get("sampling_contract_revision") or ""),
                "instances": _normalize_str_list(instances, f"{where}.instances"),
                "contracts": _normalize_contracts(item.get("contracts"), where),
                "enabled": _parse_bool(item.get("enabled", True), f"{where}.enabled"),
                "max_concurrency_per_execution": _parse_int(
                    item.get("max_concurrency_per_execution", DEFAULT_MAX_CONCURRENCY_PER_EXECUTION),
                    f"{where}.max_concurrency_per_execution",
                    minimum=1,
                    maximum=8,
                ),
            }
        )
    return normalized


def _normalize_instances(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise VideoPoolConfigError("video_instances 必须是列表")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        where = f"video_instances[{index}]"
        if not isinstance(item, dict):
            raise VideoPoolConfigError(f"{where} 必须是映射")
        _reject_unknown_keys(item, _INSTANCE_KEYS, where)
        access_mode = str(item.get("access_mode") or "").strip().lower()
        if access_mode not in ACCESS_MODES:
            raise VideoPoolConfigError(f"{where}.access_mode 必须是 exclusive/shared")
        probe = item.get("probe")
        if not isinstance(probe, dict):
            raise VideoPoolConfigError(f"{where}.probe 必须是映射")
        _reject_unknown_keys(probe, _PROBE_KEYS, f"{where}.probe")
        probe_kind = str(probe.get("kind") or "").strip()
        if probe_kind not in PROBE_KINDS:
            raise VideoPoolConfigError(f"{where}.probe.kind 不合法：{probe_kind}")
        labels = probe.get("labels") or {}
        if not isinstance(labels, dict):
            raise VideoPoolConfigError(f"{where}.probe.labels 必须是映射")
        normalized.append(
            {
                "instance_id": _require_str(item.get("instance_id"), f"{where}.instance_id"),
                "model_id": _require_str(item.get("model_id"), f"{where}.model_id"),
                "resource_key": _require_str(item.get("resource_key"), f"{where}.resource_key"),
                "access_mode": access_mode,
                "enabled": _parse_bool(item.get("enabled", False), f"{where}.enabled"),
                "driver": _require_str(item.get("driver"), f"{where}.driver"),
                "probe": {
                    "kind": probe_kind,
                    "jobs_url": _require_str(probe.get("jobs_url"), f"{where}.probe.jobs_url"),
                    "metrics_url": str(probe.get("metrics_url") or ""),
                    "labels": {str(k): str(v) for k, v in labels.items()},
                },
            }
        )
    return normalized


def _normalize_contracts(value: Any, where: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or not value:
        raise VideoPoolConfigError(f"{where}.contracts 必须是非空映射")
    result: dict[str, dict[str, Any]] = {}
    for mode, contract in value.items():
        mode_key = str(mode).strip()
        if mode_key not in CONTRACT_MODES:
            raise VideoPoolConfigError(f"{where}.contracts 含未知生成方式：{mode_key}")
        if not isinstance(contract, dict):
            raise VideoPoolConfigError(f"{where}.contracts.{mode_key} 必须是映射")
        _reject_unknown_keys(contract, _CONTRACT_KEYS, f"{where}.contracts.{mode_key}")
        task = str(contract.get("task") or "").strip()
        if task not in TASKS:
            raise VideoPoolConfigError(f"{where}.contracts.{mode_key}.task 不合法：{task}")
        duration = contract.get("duration")
        if not isinstance(duration, dict):
            raise VideoPoolConfigError(f"{where}.contracts.{mode_key}.duration 必须是映射")
        low = _parse_int(duration.get("min"), f"{where}.contracts.{mode_key}.duration.min", minimum=1)
        high = _parse_int(duration.get("max"), f"{where}.contracts.{mode_key}.duration.max", minimum=1)
        if low > high:
            raise VideoPoolConfigError(f"{where}.contracts.{mode_key}.duration 区间非法")
        result[mode_key] = {
            "task": task,
            "resolutions": _normalize_str_list(
                contract.get("resolutions"), f"{where}.contracts.{mode_key}.resolutions"
            ),
            "short_edge": _parse_int(
                contract.get("short_edge"), f"{where}.contracts.{mode_key}.short_edge", minimum=1
            ),
            "ratios": _normalize_str_list(
                contract.get("ratios"), f"{where}.contracts.{mode_key}.ratios"
            ),
            "duration": {"min": low, "max": high},
            "fps": [
                _parse_int(item, f"{where}.contracts.{mode_key}.fps", minimum=1)
                for item in _normalize_list(contract.get("fps"), f"{where}.contracts.{mode_key}.fps")
            ],
        }
    return result


def _reject_unknown_keys(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(str(key) for key in data if key not in allowed)
    if unknown:
        raise VideoPoolConfigError(f"{where} 含未知字段：{', '.join(unknown)}")


def _index_unique(
    rows: list[dict[str, Any]],
    where: str,
    key: str,
    *,
    allow_duplicates: bool = False,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = str(row.get(key) or "").strip()
        if not value:
            raise VideoPoolConfigError(f"{where} 的 {key} 不能为空")
        if value in result and not allow_duplicates:
            raise VideoPoolConfigError(f"{where} 的 {key} 重复：{value}")
        result[value] = row
    return result


def _require_str(value: Any, where: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise VideoPoolConfigError(f"{where} 不能为空")
    return text


def _normalize_list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise VideoPoolConfigError(f"{where} 必须是非空列表")
    return value


def _normalize_str_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise VideoPoolConfigError(f"{where} 必须是非空列表")
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if not text:
            raise VideoPoolConfigError(f"{where} 含空值")
        result.append(text)
    return result


def _parse_int(
    value: Any,
    where: str,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    if isinstance(value, bool) or value is None:
        raise VideoPoolConfigError(f"{where} 必须是整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise VideoPoolConfigError(f"{where} 必须是整数") from exc
    if minimum is not None and parsed < minimum:
        raise VideoPoolConfigError(f"{where} 不能小于 {minimum}")
    if maximum is not None and parsed > maximum:
        raise VideoPoolConfigError(f"{where} 不能大于 {maximum}")
    return parsed


def _parse_bool(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise VideoPoolConfigError(f"{where} 必须是布尔值")


def _mtime_ns(path: Path) -> Optional[int]:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


__all__ = [
    "ACCESS_MODES",
    "CONTRACT_MODES",
    "DEFAULT_MAX_CONCURRENCY_PER_EXECUTION",
    "DEFAULT_MAX_QUEUED_JOBS",
    "DEFAULT_MAX_RUNNING_JOBS",
    "DEFAULT_MODE",
    "DEFAULT_QUEUE_TIMEOUT_SECONDS",
    "DISPATCH_MODES",
    "PLACEHOLDER_TEXT",
    "PROBE_KINDS",
    "PoolConfigConflict",
    "TASKS",
    "VideoPoolConfigError",
    "VideoPoolConfigStore",
    "VideoPoolSnapshot",
    "collect_placeholder_issues",
    "compute_revision",
    "default_pool_config",
    "effective_instance_view",
    "effective_pool_view",
    "is_placeholder_text",
    "last_good_snapshot",
    "load_pool_config",
    "normalize_pool_config",
    "resolve_dispatch_enabled",
    "resolve_profiles_path",
    "validate_enabled_instances",
    "validate_pool_config",
    "write_pool_config",
]
