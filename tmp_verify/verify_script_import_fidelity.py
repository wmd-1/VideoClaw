# -*- coding: utf-8 -*-
"""C2 script-import-fidelity 验证（容器内执行，8 项断言）。"""

import asyncio
import json
import os
from unittest.mock import patch

from core import script_import as si
from core.agents.script_agent import ScriptWriterAgent

FIXTURES = os.environ.get("C2_FIXTURES", "tmp_verify/fixtures")
PASS = 0
FAILED = []


def check(name, condition, detail=""):
    global PASS
    if condition:
        PASS += 1
        print("PASS", name)
    else:
        FAILED.append(name)
        print("FAIL", name, detail)


def read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


CASES = {
    "script_episode_style.txt": {"episodes": 3, "format": "episode"},
    "script_preamble_episode.txt": {"episodes": 2, "format": "episode"},
    "script_act_style.txt": {"episodes": 1, "format": "act"},
    "script_scene_style.txt": {"episodes": 1, "format": "scene"},
    "script_plain.txt": {"episodes": 1, "format": "plain"},
    "script_markdown.txt": {"episodes": 1, "format": "markdown"},
    "script_noisy.txt": {"episodes": 1, "format": "act"},
}

slice_results = {}
range_results = {}
count_results = {}
meta_results = {}
parsed = {}

for name, expected in CASES.items():
    raw = read(name)
    artifact = si.build_import_artifact(raw, session_id=f"verify-{name}")
    lines = si.split_lines(raw)
    episodes = artifact["episodes"]
    parsed[name] = (raw, lines, artifact)

    ok = False
    try:
        si.validate_slices(lines, episodes)
        ok = all(
            episode["content"]
            == "\n".join(lines[episode["line_start"]:episode["line_end"] + 1])
            for episode in episodes
        )
    except Exception:
        ok = False
    slice_results[name] = ok

    range_results[name] = all(
        0 <= episode["line_start"] <= episode["line_end"] < len(lines)
        for episode in episodes
    )
    count_results[name] = (
        len(episodes) == expected["episodes"]
        and artifact["meta"]["source_format"] == expected["format"]
    )
    meta_results[name] = (
        not [
            item
            for item in artifact["line_annotations"]
            if item["type"] == "dialogue"
            and ("视频时长" in lines[item["line"]] or "视频基调" in lines[item["line"]])
        ]
        and all(
            item["type"] == "meta"
            for item in artifact["line_annotations"]
            if "第" in lines[item["line"]] and "页" in lines[item["line"]]
        )
    )

check("① 所有切片可还原基线", all(slice_results.values()), str(slice_results))
check("② episodes 行区间可定位且不越界", all(range_results.values()), str(range_results))
# Markdown 标题不得被模型升格为集（无集级候选时只接受形如集的标题）
markdown_candidates = si.detect_candidates(si.split_lines(read("script_markdown.txt")))
markdown_starts, markdown_by = si.decide_episode_starts(
    markdown_candidates, [item["line"] for item in markdown_candidates]
)
count_results["script_markdown.txt"] = (
    count_results["script_markdown.txt"]
    and markdown_starts == []
    and markdown_by == "fallback"
)
# 集级候选必须全取：模型只返回部分集号时回退 rule，不允许静默并集
episode_candidates = si.detect_candidates(si.split_lines(read("script_episode_style.txt")))
episode_lines = sorted(
    {item["line"] for item in episode_candidates if item["level"] == si.LEVEL_EPISODE}
)
partial_starts, partial_by = si.decide_episode_starts(episode_candidates, episode_lines[:2])
count_results["script_episode_style.txt"] = (
    count_results["script_episode_style.txt"]
    and partial_starts == episode_lines
    and partial_by == "rule"
)
# 前言并入首集时，首集标题仍取第一个集标记行
preamble_artifact = parsed["script_preamble_episode.txt"][2]
count_results["script_preamble_episode.txt"] = (
    count_results["script_preamble_episode.txt"]
    and "第1集" in preamble_artifact["episodes"][0]["act_title"]
)
check("③ 集数与 source_format 符合预期（含 Markdown 不升格/集号全取/首集标题）", all(count_results.values()), str(count_results))
check("④ 元信息行不被标为 dialogue 且页码标为 meta", all(meta_results.values()), str(meta_results))

act_raw, act_lines, act_artifact = parsed["script_act_style.txt"]
noisy_raw, noisy_lines, noisy_artifact = parsed["script_noisy.txt"]
uploaded_names = {item["name"] for item in act_artifact["characters"] if item["description_source"] == "uploaded"}
noisy_uploaded = {item["name"] for item in noisy_artifact["characters"] if item["description_source"] == "uploaded"}
inferred_merged = si.merge_characters(
    [{"name": "小陈", "description": "d"}],
    [
        {"name": "浙小控/浙宝箱", "description": "d"},
        {"name": "对公风控研判官", "description": "d"},
        {"name": "小周", "description": "d"},
    ],
    exclude_names=["对公风控研判官——AI赋能全流程风控实战"],
)
merged_names = [item["name"] for item in inferred_merged]
check(
    "⑤ 显式角色设定优先，推断不合写，片名子串主体保留",
    {"小陈", "小周"} <= uploaded_names
    and {"小陈", "小周"} <= noisy_uploaded
    and merged_names == ["小陈", "浙小控", "浙宝箱", "对公风控研判官", "小周"],
    f"{uploaded_names} / {merged_names}",
)

pure_narration = "【旁白（解说）】画外音内容。\n【画面描述】空镜。\n"
pure_artifact = si.build_import_artifact(pure_narration, session_id="verify-narration")
check(
    "⑥ 出镜虚拟形象提取、纯旁白不提取",
    {"浙小控", "浙宝箱"} <= uploaded_names and pure_artifact["characters"] == [],
    f"{uploaded_names} / {pure_artifact['characters']}",
)

scene_items = si.merge_settings(
    [
        {"name": "支行办公室", "description": "开放工位"},
        {"name": "网银转账界面", "description": "不应保留"},
        {"name": "屏幕显示", "description": "不应保留"},
    ]
)
char_items = si.merge_characters([{"name": "小陈", "description": "男，32岁"}], [])
check(
    "⑦ 场景过滤界面类且角色/场景均有稳定 ID",
    [item["name"] for item in scene_items] == ["支行办公室"]
    and all(item["setting_id"].startswith("set_") for item in scene_items)
    and char_items[0]["character_id"].startswith("char_"),
    str(scene_items),
)

# ⑧ 导入分支：只调用解析类，计数标记不落产物
raw = act_raw
calls = []


def fake_query(llm, prompt, image_urls=[], model="", safe_content=True, task_id=None, web_search=False):
    calls.append(prompt)
    if "候选分段行" in prompt:
        return json.dumps({"episode_start_lines": []})
    if "幕/场标题" in prompt:
        return json.dumps(
            {
                "settings": [
                    {
                        "name": "支行办公室",
                        "description": "开放工位，日光灯，木质桌面",
                        "aliases": [],
                        "heading_lines": [act_lines.index("【第二幕：授信申报——报告撰写耗时】")],
                    }
                ]
            },
            ensure_ascii=False,
        )
    if "剧本信息提取助手" in prompt:
        return json.dumps(
            {
                "title": "对公风控研判官",
                "logline": "AI 风控助手",
                "genre": ["金融"],
                "mood": "专业科技感",
                "film_type": "宣传",
                "subject_mode": "人物故事",
                "characters": [{"name": "小陈", "description": "男，32岁，深色西装", "species": "人类"}],
                "settings": [],
            },
            ensure_ascii=False,
        )
    return "{}"


agent = ScriptWriterAgent()
agent._cancellable_query = fake_query
input_data = {
    "session_id": "verify-import",
    "llm_model": "mock-model",
    "script_source": "upload",
    "web_search": False,
    "_session_meta": {"file_path": "fixture.txt", "llm_model": "mock-model"},
}
allowed_markers = ("候选分段行", "幕/场标题", "剧本信息提取助手")
forbidden_markers = ("完整剧本文本初稿", "台词评估", "情节评估", "优化剧本", "删减", "集数修复")
with patch.object(
    ScriptWriterAgent, "_resolve_uploaded_document", return_value="/tmp/fixture.txt"
), patch(
    "models.file_reader.FileReader.extract_text", return_value=raw
):
    result = asyncio.run(agent.process(input_data))

check(
    "⑧ 导入只调用解析类且 source_episode_count 不落产物",
    calls
    and all(any(marker in prompt for marker in allowed_markers) for prompt in calls)
    and not any(any(marker in prompt for marker in forbidden_markers) for prompt in calls)
    and result.get("source_episode_count") == 1
    and "source_episode_count" not in result["payload"]
    and result["payload"]["episodes"][0]["content"] == si.normalize_text(raw),
    f"calls={len(calls)}",
)

check(
    "⑨ 占位（录屏/素材）与定帧写入 meta",
    len(act_artifact["meta"].get("placeholders") or []) >= 2
    and "全链路智能风控" in (act_artifact["meta"].get("end_card") or ""),
    f"placeholders={act_artifact['meta'].get('placeholders')} end_card={act_artifact['meta'].get('end_card')!r}",
)

print(f"\nTOTAL PASS={PASS} FAIL={len(FAILED)}")
if FAILED:
    print("FAILED:", FAILED)
    raise SystemExit(1)
print("ALL PASS")
