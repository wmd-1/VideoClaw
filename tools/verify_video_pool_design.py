#!/usr/bin/env python3
"""仅校验视频池设计样例，不实现或调用生产调度器。

必须在既有 Docker 镜像内运行；命令见配套设计 §13.1。
只读取 YAML 与 Markdown，不加载 backend/config.py、不连接 DB 或模型服务。
八组 PASS 代表样例结构通过，不代表就绪解析、选池 UI 或并发功能已实现。
--self-test 用内存反例检查校验器确实能拒绝错误输入，不写文件。
"""
from __future__ import annotations

import argparse
import copy
import re
import sys
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import unquote, urlsplit


EXAMPLE_REL = Path("video-claw/video-claw/backend/video_profiles.yaml.example")
DESIGN_REL = Path("docs/designs/视频生成自动调度与实例池方案-2026-11-19.md")
EXPECTED_POOLS = {
    "h3-omni-fl2va": ["omni-01", "omni-03"],
    "h3-omni-ref2va": ["omni-02", "omni-04"],
    "h3-sglang-fl2va": ["sglang-01", "sglang-03"],
    "h3-sglang-ref2va": ["sglang-02", "sglang-04"],
}
EXPECTED_DEFAULTS = {
    "first_frame": "h3-sglang-fl2va",
    "start_end_frame": "h3-sglang-fl2va",
    "reference": "h3-sglang-ref2va",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load_mapping(text: str) -> dict:
    import yaml

    class UniqueLoader(yaml.SafeLoader):
        """不接受被普通 YAML loader 静默覆盖的重复键。"""

    def mapping(loader, node, deep=False):
        loader.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            require(key not in result, f"YAML 重复键：{key}")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    data = yaml.load(text, Loader=UniqueLoader)
    require(isinstance(data, dict), "YAML 顶层必须是对象")
    return data


def index_rows(rows: list, key: str) -> dict:
    require(isinstance(rows, list), f"{key} 所在容器必须是数组")
    result = {}
    for row in rows:
        require(isinstance(row, dict), f"{key} 条目必须是对象")
        value = row.get(key)
        require(isinstance(value, str) and bool(value.strip()), f"{key} 不能为空")
        require(value not in result, f"{key} 重复：{value}")
        result[value] = row
    return result


@dataclass
class Inputs:
    data: dict
    design: str
    design_path: Path


def check_counts(ctx: Inputs) -> None:
    d = ctx.data
    require(set(d) == {"api_providers", "custom_models", "video_dispatch", "video_profiles", "video_instances"},
            "样例顶层分区不符合约定")
    providers = d["api_providers"]
    require(isinstance(providers, dict) and len(providers) == 8, "必须有八个供应商")
    require(Counter(p["protocol"] for p in providers.values()) == {"vllm-omni": 4, "sglang": 4},
            "协议必须各四个")
    require(len(index_rows(d["custom_models"], "id")) == 8, "必须有八个唯一模型")
    require(len(index_rows(d["video_instances"], "instance_id")) == 8, "必须有八个唯一实例")
    require(len(index_rows(d["video_profiles"], "id")) == 4, "必须有四个唯一池")


def check_defaults(ctx: Inputs) -> None:
    d = ctx.data
    require("enabled" not in d["video_dispatch"], "开关必须只在 .env，YAML 不得写 enabled")
    require(d["video_dispatch"]["default_mode"] == "auto", "默认模式必须为 auto")
    require(d["video_dispatch"]["queue_timeout_seconds"] == 3600, "样例队列等待上限须为 3600")
    require(d["video_dispatch"]["max_queued_jobs"] == 1000, "样例队列容量须为 1000")
    require(d["video_dispatch"]["max_running_jobs"] == 8, "样例全局在途上限须为 8")
    require(all(i["enabled"] is False for i in d["video_instances"]), "样例实例不得启用")
    for m in d["custom_models"]:
        require(type(m["concurrency"]) is int and m["concurrency"] == 1, "模型须为单并发整数 1")
        require(m["video_api_mode"] == "async", "样例模型必须 async")
    require(all(p.get("enabled", True) is True for p in d["video_profiles"]), "样例池不应设置 enabled: false")


def check_references(ctx: Inputs) -> None:
    d = ctx.data
    models = index_rows(d["custom_models"], "id")
    instances = index_rows(d["video_instances"], "instance_id")
    require(all(m["provider"] in d["api_providers"] for m in models.values()), "存在悬空 provider")
    require(all(i["model_id"] in models for i in instances.values()), "存在悬空 model_id")
    require(len({i["resource_key"] for i in instances.values()}) == 8, "本样例须有八个独立资源键")
    require(len({i["model_id"] for i in instances.values()}) == 8, "八实例须各引用一个模型")
    for p in d["video_profiles"]:
        require(all(i in instances for i in p["instances"]), f"池 {p['id']} 存在悬空实例")


def check_members(ctx: Inputs) -> None:
    d = ctx.data
    models = index_rows(d["custom_models"], "id")
    instances = index_rows(d["video_instances"], "instance_id")
    pools = index_rows(d["video_profiles"], "id")
    require({k: p["instances"] for k, p in pools.items()} == EXPECTED_POOLS, "四池成员不匹配")
    require(Counter(i for p in pools.values() for i in p["instances"]) == Counter(instances.keys()),
            "样例成员有重复归属或遗漏")
    for pool_id, p in pools.items():
        task = pool_id.rsplit("-", 1)[1]
        modes = {"first_frame", "start_end_frame"} if task == "fl2va" else {"reference"}
        require(set(p["contracts"]) == modes, f"{pool_id} 模式契约不符")
        protocol = "vllm-omni" if "-omni-" in pool_id else "sglang"
        for iid in p["instances"]:
            model = models[instances[iid]["model_id"]]
            require(d["api_providers"][model["provider"]]["protocol"] == protocol, "池内协议不匹配")
            caps = model["capabilities"]
            require(model["types"] == ["video"] and caps["tasks"] == [task], "实例任务不匹配")
            require(caps["allow_frame_as_reference"] is False, "样例不得授权帧图语义降级")
            for contract in p["contracts"].values():
                require(contract["task"] == task, "池契约 task 不匹配")
                for key in ("short_edge", "resolutions", "duration", "fps"):
                    require(contract[key] == caps[key], f"池/模型 {key} 不一致")


def check_recommendations(ctx: Inputs) -> None:
    d = ctx.data
    pools = index_rows(d["video_profiles"], "id")
    require(d["video_dispatch"]["default_profiles"] == EXPECTED_DEFAULTS, "样例推荐映射不符合约定")
    for mode, pid in d["video_dispatch"]["default_profiles"].items():
        require(pid in pools and mode in pools[pid]["contracts"], "默认池引用或模式不合法")
    # 仅解析文档中的运行时 YAML 空默认片段；不导入业务加载器来冒充已实施。
    # 固定契约耦合点：下述中文标记后首个 YAML 块。改标题或插入前置 YAML 时须同步这里。
    marker = "首次启动的资源池最小默认片段"
    require(marker in ctx.design, "设计缺少首次启动默认片段")
    block = re.search(r"```yaml\n(.*?)\n```", ctx.design.split(marker, 1)[1], re.S)
    require(block is not None, "首次启动默认片段须为 YAML")
    require(load_mapping(block.group(1)) == {
        "video_dispatch": {
            "default_mode": "auto",
            "default_profiles": {},
            "queue_timeout_seconds": 3600,
            "max_queued_jobs": 1000,
            "max_running_jobs": 8,
        },
        "video_profiles": [], "video_instances": [],
    }, "文档运行时默认必须为空推荐和空资源列表，且开关只在 .env")
    for key in ("VC_VIDEO_DISPATCH__ENABLED", "VC_VIDEO_PROFILES_FILE"):
        require(key in ctx.design, f"设计缺少环境变量说明：{key}")


def check_zero_ready(ctx: Inputs) -> None:
    d = ctx.data
    instances = index_rows(d["video_instances"], "instance_id")
    enabled_members = [p for p in d["video_profiles"] if any(instances[i]["enabled"] for i in p["instances"])]
    require(not enabled_members, "样例不应有具备启用成员必要条件的池")
    # 这里只验证样例仍使用保留地址和待填值，不实现生产占位域名守卫或资格解析。
    for p in d["api_providers"].values():
        host = (urlsplit(p["base_url"]).hostname or "").rstrip(".").lower()
        require(host == "example" or host.endswith(".example"), "样例只能使用保留域名")
        require(p["api_key"] == "", "样例不能保存真实凭据")
    for p in d["video_profiles"]:
        for key in ("artifact_revision", "prompt_contract_revision", "sampling_contract_revision"):
            require(p[key] == "CHANGE_ME", f"样例 {key} 应保留待填标记")


def check_no_runtime_state(ctx: Inputs) -> None:
    forbidden = {"state", "ready", "qualified", "dispatchable", "observation", "selection_source"}

    def walk(value):
        if isinstance(value, dict):
            require(not (forbidden & value.keys()), "配置不得持久化运行态/执行选择来源")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(ctx.data)
    models = index_rows(ctx.data["custom_models"], "id")
    for i in ctx.data["video_instances"]:
        p = ctx.data["api_providers"][models[i["model_id"]]["provider"]]
        base = p["base_url"].rstrip("/")
        require(i["probe"]["jobs_url"] == base + "/videos", "作业地址须绑定同一实例")
        require(i["probe"]["metrics_url"] == base.removesuffix("/v1") + "/metrics", "指标地址不匹配实例")
        if p["protocol"] == "sglang":
            require(i["driver"] == "sglang_async" and i["probe"]["kind"] == "sglang_diffusion", "SGLang 驱动错误")
            require(i["probe"]["labels"] == {"role": "monolithic", "replica": "CHANGE_ME"}, "SGLang 标签需保留待核对值")
        else:
            require(i["driver"] == "vllm_omni_async" and i["probe"]["kind"] == "vllm_omni", "Omni 驱动错误")


def check_document(ctx: Inputs) -> None:
    links = re.findall(r"\]\(([^)]+)\)", ctx.design)
    require(bool(links), "设计须提供相关文件链接")
    for link in links:
        parsed = urlsplit(link)
        if not parsed.scheme and parsed.path:
            require((ctx.design_path.parent / unquote(parsed.path)).exists(), f"文档链接不存在：{link}")
    require(len(re.findall(r"^```", ctx.design, re.M)) % 2 == 0, "Markdown 代码块未闭合")
    for old in ("默认关闭", "旧项目", "旧会话", "不静默迁移", "按 manual 解释", "保留其占位"):
        require(old not in ctx.design, f"设计残留过时口径：{old}")


CHECKS = (
    ("G01 YAML 结构、唯一 ID 与 4+4 数量", check_counts),
    ("G02 开关在 .env、YAML auto、八实例停用与 async/单并发", check_defaults),
    ("G03 引用完整性与八份独立样例资源", check_references),
    ("G04 四池精确归属与任务/参数契约", check_members),
    ("G05 样例推荐有效、文档运行时默认空推荐", check_recommendations),
    ("G06 零池具备启用成员必要条件、保留地址", check_zero_ready),
    ("G07 不持久化运行态、驱动和探针引用", check_no_runtime_state),
    ("G08 文档链接、代码块与过时口径", check_document),
)


def self_test(ctx: Inputs) -> None:
    """每组注入一个可定位反例，防止校验器只报固定 PASS。"""
    cases = []
    for _ in CHECKS:
        cases.append(replace(ctx, data=copy.deepcopy(ctx.data)))
    cases[0].data["api_providers"].pop("pool_omni_01")
    cases[1].data["video_dispatch"]["enabled"] = False
    cases[2].data["custom_models"][0]["provider"] = "missing-provider"
    cases[3].data["video_profiles"][0]["instances"] = ["omni-02", "omni-03"]
    cases[4].data["video_dispatch"]["default_profiles"]["first_frame"] = "missing-pool"
    cases[5].data["video_instances"][0]["enabled"] = True
    cases[6].data["video_profiles"][0]["ready"] = True
    cases[7] = replace(cases[7], design=ctx.design + "\n保留其占位\n")
    for (label, check), bad in zip(CHECKS, cases):
        try:
            check(bad)
        except ValueError:
            continue
        raise ValueError(f"反例未被拒绝：{label}")
    try:
        load_mapping("enabled: true\nenabled: false\n")
    except ValueError:
        return
    raise ValueError("重复 YAML 键未被拒绝")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--example", type=Path, help="替代样例路径，供独立复核使用")
    parser.add_argument("--design", type=Path, help="替代设计文档路径，供独立复核使用")
    parser.add_argument("--self-test", action="store_true", help="追加内存反例检查，不写文件")
    args = parser.parse_args()
    if not Path("/.dockerenv").exists() and not Path("/run/.containerenv").exists():
        print("FAIL 必须在项目已有 Docker 环境执行，参见设计 §13.1", file=sys.stderr)
        return 2
    label = "读取输入"
    try:
        example = args.example or args.repo_root / EXAMPLE_REL
        design_path = args.design or args.repo_root / DESIGN_REL
        ctx = Inputs(load_mapping(example.read_text(encoding="utf-8")), design_path.read_text(encoding="utf-8"), design_path)
        for label, check in CHECKS:
            check(ctx)
            print(f"PASS {label}", flush=True)
        if args.self_test:
            label = "校验器内存反例"
            self_test(ctx)
            print("反例检查通过：8 个分组反例 + 1 个重复键反例")
    except Exception as exc:
        print(f"FAIL {label}: {exc}", file=sys.stderr)
        return 1
    print("结果：8/8 样例分组通过；调度器、就绪解析、UI 与八用户并发仍为运行待验。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
