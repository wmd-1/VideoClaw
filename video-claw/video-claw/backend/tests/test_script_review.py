# -*- coding: utf-8 -*-
"""剧本 AI 质检：建议规范化、逐行应用与前后端预览契约。

只允许在既有后端容器内执行（详见 README「测试与校验」）。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core import script_review, script_revision
from core.agents.script_agent import ScriptWriterAgent
from prompts.loader import load_prompt


def _artifact(content: str = "第一行\n第二行\n第三行") -> dict:
    return {
        "title": "测试剧本",
        "episodes": [
            {"episode_number": 1, "act_title": "第1集", "content": content},
        ],
        "characters": [{"name": "小陈", "description": "青年"}],
        "settings": [{"name": "办公室", "description": "内景"}],
        "line_annotations": [{"line": 0, "type": "action"}],
        "scene_spans": [{"line": 0, "setting": "办公室"}],
        "meta": {"language": "zh", "source_format": "episode"},
    }


def test_build_numbered_episodes_is_one_based():
    text = script_review.build_numbered_episodes(_artifact()["episodes"])
    assert "第1集《第1集》" in text
    assert "1: 第一行" in text
    assert "3: 第三行" in text


def test_normalize_review_filters_and_dedupes():
    raw = {
        "summary": "有几处需要改",
        "issues": [
            {"episode_number": 1, "line": 1, "type": "dialogue", "severity": "high",
             "problem": "台词生硬", "suggestion": "口语化", "action": "replace", "text": "新第一行"},
            {"episode_number": 9, "line": 1, "problem": "不存在的集", "action": "replace", "text": "X"},
            {"episode_number": 1, "line": 99, "problem": "不存在的行", "action": "replace", "text": "X"},
            {"episode_number": 1, "line": 1, "problem": "重复替换", "action": "replace", "text": "Y"},
            {"episode_number": 1, "line": 2, "type": "description", "severity": "low",
             "problem": "缺画面描述", "action": "insert_after", "text": "第二行"},
            {"episode_number": 1, "line": 3, "problem": "多余行", "action": "delete", "text": "会被忽略"},
            {"episode_number": 1, "line": 2, "problem": "省略 action", "text": "默认改写"},
            {"episode_number": 1, "line": 2, "problem": "", "action": "replace", "text": "空问题"},
        ],
    }
    normalized = script_review.normalize_review(raw, _artifact()["episodes"])
    assert normalized is not None
    issues = normalized["issues"]
    assert [issue["line"] for issue in issues] == [1, 2, 2, 3]
    assert [issue["action"] for issue in issues] == ["replace", "insert_after", "replace", "delete"]
    assert [issue["id"] for issue in issues] == ["qr_1", "qr_2", "qr_3", "qr_4"]
    assert issues[0]["original"] == "第一行"
    # 省略 action 但有文本：按 replace 收下，与同行的 insert_after 不冲突
    assert issues[2]["line"] == 2 and issues[2]["action"] == "replace"
    assert issues[3]["line"] == 3 and issues[3]["action"] == "delete" and issues[3]["text"] == ""


def test_apply_issue_edits_replace_insert_delete():
    cases = [
        {"id": "qr_1", "episode_number": 1, "line": 2, "action": "replace",
         "original": "第二行", "text": "第二行A\n第二行B"},
        {"id": "qr_2", "episode_number": 1, "line": 3, "action": "insert_after",
         "original": "第三行", "text": "新增行"},
        {"id": "qr_3", "episode_number": 1, "line": 1, "action": "delete",
         "original": "第一行", "text": ""},
    ]
    updated = script_review.apply_issue_edits(_artifact()["episodes"], cases)
    assert updated[0]["content"] == "第二行A\n第二行B\n第三行\n新增行"
    original = _artifact()["episodes"]
    assert original[0]["content"] == "第一行\n第二行\n第三行", "不得改写传入产物"


def test_normalize_review_accepts_explicit_empty_issues():
    assert script_review.normalize_review({"issues": []}, _artifact()["episodes"]) == {
        "summary": "",
        "issues": [],
    }
    assert script_review.normalize_review({}, _artifact()["episodes"]) is None
    assert script_review.normalize_review(
        {"issues": [{"episode_number": 1, "line": 99, "problem": "越界"}]},
        _artifact()["episodes"],
    ) is None, "原始 issues 非空但全部不可定位时必须判为不可用"


def test_normalize_review_rejects_punctuation_width_only_replace():
    episodes = [{"episode_number": 1, "content": "你好，世界。\n第二行"}]
    raw = {
        "summary": "只改标点",
        "issues": [
            {"episode_number": 1, "line": 1, "problem": "标点", "action": "replace", "text": "你好,世界."},
            {"episode_number": 1, "line": 2, "problem": "真正改写", "action": "replace", "text": "第二行（改）"},
        ],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert [item["line"] for item in normalized["issues"]] == [2]


def test_normalize_review_rejects_blank_and_metadata_targets():
    episodes = [{
        "episode_number": 1,
        "content": "视频时长：60秒\n\n字幕：一句字幕\n角色设定：小李\n小刘：正常台词行",
    }]
    raw = {
        "summary": "元信息与空行",
        "issues": [
            {"episode_number": 1, "line": 1, "problem": "删时长", "action": "delete"},
            {"episode_number": 1, "line": 2, "problem": "删空行", "action": "delete"},
            {"episode_number": 1, "line": 3, "problem": "删字幕", "action": "delete"},
            {"episode_number": 1, "line": 4, "problem": "改角色设定", "action": "replace", "text": "角色设定：小王"},
            {"episode_number": 1, "line": 5, "problem": "正常改写", "action": "replace", "text": "小刘：正常台词行（改）"},
        ],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert [item["line"] for item in normalized["issues"]] == [5]


def test_normalize_review_rejects_cross_kind_and_speaker_mismatch():
    episodes = [{
        "episode_number": 1,
        "content": "字幕： 先解决情绪\n小刘（温和）：您好，请坐",
    }]
    raw = {
        "summary": "错行替换",
        "issues": [
            {"episode_number": 1, "line": 1, "problem": "字幕换成对白", "action": "replace", "text": "李先生：您好"},
            {"episode_number": 1, "line": 2, "problem": "换说话人", "action": "replace", "text": "李先生：您好"},
        ],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert normalized["issues"] == []

    valid = {
        "summary": "合法拆分",
        "issues": [
            {"episode_number": 1, "line": 1, "problem": "字幕拆分", "action": "replace", "text": "字幕： 先解决情绪\n字幕： 再解决问题"},
            {"episode_number": 1, "line": 2, "problem": "台词拆分", "action": "replace", "text": "小刘（温和）：您好\n小刘：请坐"},
        ],
    }
    normalized = script_review.normalize_review(valid, episodes)
    assert normalized is not None
    assert [item["line"] for item in normalized["issues"]] == [1, 2]


def test_normalize_review_rejects_ungrounded_insert_and_caps_per_line():
    original = "小刘微笑着接过卡片和身份证，看了一眼电脑屏幕。"
    episodes = [{"episode_number": 1, "content": original}]
    raw = {
        "summary": "插入接地",
        "issues": [
            {"episode_number": 1, "line": 1, "problem": "凭空新增", "action": "insert_after",
             "text": "小刘将卡片递还给客户，客户点头致谢。"},
            {"episode_number": 1, "line": 1, "problem": "接地1", "action": "insert_after",
             "text": "小刘微笑着接过卡片"},
            {"episode_number": 1, "line": 1, "problem": "接地2", "action": "insert_after",
             "text": "看了一眼电脑屏幕"},
            {"episode_number": 1, "line": 1, "problem": "接地3超限", "action": "insert_after",
             "text": "小刘微笑着接过身份证"},
        ],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert [item["text"] for item in normalized["issues"]] == [
        "小刘微笑着接过卡片",
        "看了一眼电脑屏幕",
    ]


def test_normalize_review_protects_markdown_marker_and_new_markers():
    episodes = [{
        "episode_number": 1,
        "content": "**【画面描述】】 月底某天，客户来到网点。\n小刘：小刘微笑着接待客户。",
    }]
    raw = {
        "summary": "markdown 标记",
        "issues": [
            {"episode_number": 1, "line": 1, "problem": "改 markdown 标记", "action": "replace",
             "text": "【画面描述】 月底某天，客户来到网点。"},
            {"episode_number": 1, "line": 2, "problem": "加新标记", "action": "replace",
             "text": "【画面描述】 小刘微笑着接待客户。"},
            {"episode_number": 1, "line": 2, "problem": "正常改写", "action": "replace",
             "text": "小刘：小刘微笑着接待顾客。"},
        ],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert [item["line"] for item in normalized["issues"]] == [2]
    assert normalized["issues"][0]["text"] == "小刘：小刘微笑着接待顾客。"


def test_normalize_review_caps_replace_lines():
    original = (
        "小刘（温和）：您好。请出示身份证和银行卡。"
        "我先帮您核对资料。我马上为您办理。"
    )
    episodes = [{"episode_number": 1, "content": original}]
    raw = {
        "summary": "拆分段数",
        "issues": [
            {"episode_number": 1, "line": 1, "problem": "四段", "action": "replace",
             "text": "小刘（温和）：您好。\n小刘（温和）：请出示身份证和银行卡。\n小刘（温和）：我先帮您核对资料。\n小刘（温和）：我马上为您办理。"},
            {"episode_number": 1, "line": 1, "problem": "三段", "action": "replace",
             "text": "小刘（温和）：您好。\n小刘（温和）：请出示身份证和银行卡。\n小刘（温和）：我先帮您核对资料。"},
        ],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert len(normalized["issues"]) == 1
    assert normalized["issues"][0]["text"].count("\n") == 2


def test_normalize_review_rejects_fabricated_dialogue_continuation():
    episodes = [{"episode_number": 1, "content": "小刘（温和）：您好，请坐。"}]
    text = (
        "小刘（温和）：您好，请坐。\n"
        "小刘（温和）：顺便通知您中了大奖并赠送一台全新轿车。"
    )
    raw = {
        "summary": "凭空对白",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "凭空对白",
            "action": "replace", "text": text,
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert normalized["issues"] == []


def test_normalize_review_rejects_recombined_dialogue_by_length():
    episodes = [{
        "episode_number": 1,
        "content": "小刘（温和）：您好请坐请问办理什么业务",
    }]
    text = "小刘（温和）：请问您坐好，我这就为您办理什么业务并保证满意。"
    raw = {
        "summary": "字符重组",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "字符重组",
            "action": "replace", "text": text,
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert normalized["issues"] == []


def test_normalize_review_allows_short_dialogue_growth():
    cases = [
        ("小刘：好的。", "小刘：好的呀。"),
        ("小刘：请坐。", "小刘：请您先坐下。"),
    ]
    for original, text in cases:
        normalized = script_review.normalize_review(
            {
                "summary": "短句润色",
                "issues": [{
                    "episode_number": 1, "line": 1, "problem": "短句润色",
                    "action": "replace", "text": text,
                }],
            },
            [{"episode_number": 1, "content": original}],
        )
        assert normalized is not None
        assert [item["text"] for item in normalized["issues"]] == [text]


def test_normalize_review_rejects_recombined_action_by_length():
    original = "小刘微笑着接过卡片和身份证，看了一眼电脑屏幕。"
    text = original + "又看了一眼身份证和卡片。"
    normalized = script_review.normalize_review(
        {
            "summary": "动作重组",
            "issues": [{
                "episode_number": 1, "line": 1, "problem": "动作重组",
                "action": "replace", "text": text,
            }],
        },
        [{"episode_number": 1, "content": original}],
    )
    assert normalized is not None
    assert normalized["issues"] == []


def test_normalize_review_rejects_episode_collage_for_dialogue():
    episodes = [{
        "episode_number": 1,
        "content": (
            "小刘（温和）：您好，请坐。\n"
            "通知：您中了大奖，赠送全新轿车。"
        ),
    }]
    text = (
        "小刘（温和）：您好，请坐。\n"
        "小刘（温和）：您中了大奖，赠送全新轿车。"
    )
    raw = {
        "summary": "集内拼凑",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "集内拼凑",
            "action": "replace", "text": text,
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert normalized["issues"] == []


def test_normalize_review_allows_small_dialogue_polish():
    episodes = [{"episode_number": 1, "content": "小刘（温和）：您好，请坐。"}]
    text = "小刘（温和）：您好，请坐，请。"
    raw = {
        "summary": "对白润色",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "对白润色",
            "action": "replace", "text": text,
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert [item["text"] for item in normalized["issues"]] == [text]


def test_normalize_review_rejects_action_replace_without_prefix():
    episodes = [{"episode_number": 1, "content": "小刘微笑着接过卡片和身份证。"}]
    raw = {
        "summary": "动作行续行无前缀",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "续行无前缀",
            "action": "replace",
            "text": "小刘微笑着接过卡片和身份证。\n小刘看了一眼电脑屏幕。",
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert normalized["issues"] == []


def test_normalize_review_allows_action_replace_with_action_tag():
    episodes = [{"episode_number": 1, "content": "小刘微笑着接过卡片和身份证，看了一眼电脑屏幕。"}]
    text = "小刘微笑着接过卡片和身份证。\n<action>看了一眼电脑屏幕</action>"
    raw = {
        "summary": "动作行显式标签",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "显式 action",
            "action": "replace", "text": text,
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert [item["text"] for item in normalized["issues"]] == [text]


def test_clean_text_strips_trailing_newline():
    episodes = [{"episode_number": 1, "content": "小刘：正常台词内容"}]
    raw = {
        "summary": "尾换行",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "尾换行",
            "action": "replace", "text": "小刘：正常台词内容（改）\n",
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert normalized["issues"][0]["text"] == "小刘：正常台词内容（改）"


def test_grounding_rejects_prefixed_fabricated_insert():
    cases = [
        (
            "小刘（点头）：老卡也一样。",
            "小刘（点头）：凭空新增一段完全没有依据的台词。",
        ),
        (
            "小刘（解释）：系统会自动调低限额。",
            "小刘（解释）：这是原文完全没有的新政策说明。",
        ),
        (
            "画面定格，弹出服务规范字幕。",
            "小刘将银行卡和身份证递还给李阿姨。",
        ),
    ]
    for original, insert in cases:
        normalized = script_review.normalize_review(
            {
                "summary": "前缀绕过",
                "issues": [{
                    "episode_number": 1, "line": 1, "problem": "凭空新增",
                    "action": "insert_after", "text": insert,
                }],
            },
            [{"episode_number": 1, "content": original}],
        )
        assert normalized is not None
        assert normalized["issues"] == [], (original, insert)


def test_grounding_allows_same_speaker_continuation():
    original = "小刘（点头）：老卡也一样。系统会自动调低限额。"
    insert = "小刘（点头）：系统会自动调低限额。"
    normalized = script_review.normalize_review(
        {
            "summary": "有据拆分",
            "issues": [{
                "episode_number": 1, "line": 1, "problem": "拆分",
                "action": "insert_after", "text": insert,
            }],
        },
        [{"episode_number": 1, "content": original}],
    )
    assert normalized is not None
    assert [item["text"] for item in normalized["issues"]] == [insert]


def test_grounding_rejects_prefixed_insert_after_action_line():
    original = "小刘刚把卡还给李阿姨，王大哥快步走到旁边的柜台，神色匆忙。"
    insert = "小刘：旁边的柜台台面光线明亮，电脑和证件材料摆放整齐。"
    normalized = script_review.normalize_review(
        {
            "summary": "动作行后夹带台词",
            "issues": [{
                "episode_number": 1, "line": 1, "problem": "夹带",
                "action": "insert_after", "text": insert,
            }],
        },
        [{"episode_number": 1, "content": original}],
    )
    assert normalized is not None
    assert normalized["issues"] == []


def test_grounding_allows_grounded_prefixed_insert_after_action_line():
    original = "小刘刚把卡还给李阿姨，旁边的柜台台面光线明亮。"
    insert = "小刘：旁边的柜台台面光线明亮。"
    normalized = script_review.normalize_review(
        {
            "summary": "有据补充",
            "issues": [{
                "episode_number": 1, "line": 1, "problem": "补充",
                "action": "insert_after", "text": insert,
            }],
        },
        [{"episode_number": 1, "content": original}],
    )
    assert normalized is not None
    assert [item["text"] for item in normalized["issues"]] == [insert]


def test_normalize_review_rejects_replace_with_different_speaker_continuation():
    episodes = [{"episode_number": 1, "content": "小刘（温和）：您好"}]
    raw = {
        "summary": "续行换人",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "续行换人",
            "action": "replace", "text": "小刘（温和）：您好\n李先生：请坐",
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert normalized["issues"] == []


def test_normalize_review_rejects_replace_with_unprefixed_continuation():
    episodes = [{"episode_number": 1, "content": "小刘（温和）：您好，请坐。"}]
    raw = {
        "summary": "续行无前缀",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "续行无前缀",
            "action": "replace",
            "text": "小刘（温和）：您好，请坐。\n请问有什么可以帮您？",
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert normalized["issues"] == []


def test_normalize_review_rejects_ungrounded_action_continuation():
    episodes = [{"episode_number": 1, "content": "小刘（温和）：您好"}]
    text = "小刘（温和）：您好\n<action>小刘点头</action>"
    raw = {
        "summary": "动作续行",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "动作续行",
            "action": "replace", "text": text,
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert normalized["issues"] == []


def test_normalize_review_drops_noop_replace():
    episodes = [{"episode_number": 1, "content": "小刘：您好。"}]
    raw = {
        "summary": "空转",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "空转",
            "action": "replace", "text": "小刘：您好。",
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert normalized["issues"] == []


def test_normalize_review_rejects_ungrounded_action_replace():
    episodes = [{
        "episode_number": 1,
        "content": "小刘看了一眼电脑屏幕，又低头核对材料。",
    }]
    text = (
        "<action>小刘看了一眼电脑屏幕。</action>\n"
        "<action>小刘起身去后台调取一份从未提到的机密档案并交给客户。</action>"
    )
    raw = {
        "summary": "动作注入",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "动作注入",
            "action": "replace", "text": text,
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert normalized["issues"] == []


def test_normalize_review_allows_grounded_action_split():
    episodes = [{
        "episode_number": 1,
        "content": "小刘看了一眼电脑屏幕，又低头核对材料。",
    }]
    text = (
        "<action>小刘看了一眼电脑屏幕。</action>\n"
        "<action>又低头核对材料。</action>"
    )
    raw = {
        "summary": "有据拆分",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "有据拆分",
            "action": "replace", "text": text,
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert [item["text"] for item in normalized["issues"]] == [text]


@pytest.mark.parametrize("original,text", [
    (
        "小刘微笑着接过卡片和身份证，看了一眼电脑屏幕。",
        "小刘在柜台内微笑着接过银行卡和身份证，看了一眼电脑屏幕。",
    ),
    (
        "小刘微笑着看向镜头。",
        "小刘站在柜台后微笑着看向镜头。",
    ),
])
def test_normalize_review_rejects_inferred_action_details(original, text):
    episodes = [{"episode_number": 1, "content": original}]
    raw = {
        "summary": "推断画面",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "新增画面",
            "action": "replace", "text": text,
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert normalized["issues"] == []


def test_normalize_review_allows_action_small_polish():
    episodes = [{
        "episode_number": 1,
        "content": "小刘微笑着接过卡片和身份证，看了一眼电脑屏幕。",
    }]
    text = "小刘微笑着接过银行卡和身份证，看了一眼电脑屏幕。"
    raw = {
        "summary": "动作润色",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "动作润色",
            "action": "replace", "text": text,
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert [item["text"] for item in normalized["issues"]] == [text]


def test_normalize_review_rejects_whole_new_action():
    episodes = [{
        "episode_number": 1,
        "content": "小刘微笑着接过卡片和身份证。",
    }]
    text = "小刘起身去后台调取一份从未提到的机密档案并交给客户。"
    raw = {
        "summary": "整段新动作",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "整段新动作",
            "action": "replace", "text": text,
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert normalized["issues"] == []


def test_review_size_allows_multi_episode_within_limits():
    from core.agents.script_agent import review_episode_size

    episodes = [
        {"episode_number": 1, "content": "字" * 4500},
        {"episode_number": 2, "content": "字" * 4500},
    ]
    assert review_episode_size(episodes) == (9000, 4500)


def test_review_size_rejects_oversized_episode():
    from core.agents.script_agent import review_episode_size

    episodes = [{"episode_number": 1, "content": "字" * 8001}]
    with pytest.raises(ValueError, match="单集质检上限"):
        review_episode_size(episodes)


def test_review_size_rejects_oversized_total():
    from core.agents.script_agent import review_episode_size

    episodes = [
        {"episode_number": 1, "content": "字" * 6000},
        {"episode_number": 2, "content": "字" * 6000},
    ]
    with pytest.raises(ValueError, match="总上限"):
        review_episode_size(episodes)


def test_review_script_rejects_oversized_script():
    current = _artifact("字" * 8001)
    with pytest.raises(ValueError, match="单集质检上限"):
        asyncio.run(ScriptWriterAgent()._run_review_script(
            {
                "session_id": "big_sid",
                "llm_model": "fake-model",
                "_session_meta": {"language": "zh"},
                "_session_artifacts": {"script_generation": current},
            },
            {"action": "review_script"},
        ))


def test_review_timeout_scales_with_script_chars():
    from core.agents.script_agent import script_review_timeout_for_chars

    assert script_review_timeout_for_chars(1000) == 600.0
    assert script_review_timeout_for_chars(6000) == 1000.0
    assert script_review_timeout_for_chars(8000) == pytest.approx(8000 / 6.0)
    assert script_review_timeout_for_chars(12000) == 1800.0
    assert script_review_timeout_for_chars(20000) == 1800.0


def test_apply_issue_edits_rejects_stale_original():
    cases = [
        {"id": "qr_1", "episode_number": 1, "line": 2, "action": "replace",
         "original": "已经被改过的行", "text": "新内容"},
    ]
    with pytest.raises(ValueError, match="已变化"):
        script_review.apply_issue_edits(_artifact()["episodes"], cases)


def test_apply_issue_edits_requires_selection():
    with pytest.raises(ValueError, match="至少选择"):
        script_review.apply_issue_edits(_artifact()["episodes"], [])


class _FakeLLM:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[tuple[tuple, dict]] = []

    def query(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.reply


def _stub_review_reply() -> str:
    return json.dumps(
        {
            "summary": "台词可以更口语化",
            "issues": [
                {
                    "episode_number": 1,
                    "line": 2,
                    "type": "dialogue",
                    "severity": "medium",
                    "problem": "台词生硬",
                    "suggestion": "改为更自然的表达",
                    "action": "replace",
                    "text": "第二行（改）",
                }
            ],
        },
        ensure_ascii=False,
    )


def test_review_script_builds_preview_without_touching_body(monkeypatch):
    import models.llm_client as llm_client

    current = _artifact()
    fake = _FakeLLM(_stub_review_reply())
    monkeypatch.setattr(llm_client, "LLM", lambda: fake)
    agent = ScriptWriterAgent()
    events: list[tuple[str, float]] = []
    agent.set_progress_callback(
        lambda phase, step, percent, data=None: events.append((phase, percent))
    )
    result = asyncio.run(agent._run_review_script(
        {
            "session_id": "review_sid",
            "llm_model": "fake-model",
            "_session_meta": {"language": "zh"},
            "_session_artifacts": {"script_generation": current},
        },
        {"action": "review_script"},
    ))

    assert result["requires_intervention"] is True
    assert result["stage_completed"] is False
    payload = result["payload"]
    assert payload["episodes"] == current["episodes"], "质检预览不得改写正文"
    preview = payload["pending_review"]
    assert preview["base_version"] == script_revision.content_fingerprint(current)
    assert preview["issues"][0]["id"] == "qr_1"
    assert preview["issues"][0]["original"] == "第二行"
    assert "pending_revision" not in payload
    # 质检会写回正文：必须关闭 full_to_half，保留原文全角标点/省略号
    assert fake.calls and fake.calls[0][0][3] is False
    percents = [percent for _phase, percent in events]
    assert percents == sorted(percents), "进度必须单调递增"
    assert {5, 10, 20, 80, 96, 100} <= set(percents)


def test_revise_call_disables_safe_content(monkeypatch):
    """修订同样会写回正文，必须关闭 full_to_half 以保留全角标点。"""
    import models.llm_client as llm_client

    current = _artifact()
    reply = json.dumps(
        {
            "summary": "改一处",
            "episodes": [{
                "episode_number": 1,
                "act_title": "第1集",
                "content": "第一行\n第二行（改）\n第三行",
            }],
            "characters": current["characters"],
            "settings": current["settings"],
        },
        ensure_ascii=False,
    )
    fake = _FakeLLM(reply)
    monkeypatch.setattr(llm_client, "LLM", lambda: fake)
    agent = ScriptWriterAgent()
    agent.set_progress_callback(lambda *args, **kwargs: None)
    result = asyncio.run(agent._run_revise_script(
        {
            "session_id": "revise_sid",
            "llm_model": "fake-model",
            "_session_meta": {"language": "zh"},
            "_session_artifacts": {"script_generation": current},
        },
        {"instruction": "改一处", "scope": "all"},
    ))
    assert result["requires_intervention"] is True
    assert fake.calls and fake.calls[0][0][3] is False


def test_apply_script_review_applies_only_selected():
    current = _artifact()
    preview = {
        "summary": "两条建议",
        "base_version": script_revision.content_fingerprint(current),
        "issues": [
            {"id": "qr_1", "episode_number": 1, "line": 2, "type": "dialogue",
             "severity": "high", "problem": "生硬", "suggestion": "口语化",
             "action": "replace", "original": "第二行", "text": "第二行（改）"},
            {"id": "qr_2", "episode_number": 1, "line": 3, "type": "structure",
             "severity": "medium", "problem": "缺收尾", "suggestion": "补一行",
             "action": "insert_after", "original": "第三行", "text": "新增收尾"},
        ],
    }
    result = asyncio.run(ScriptWriterAgent()._run_apply_script_review(
        {
            "session_id": "apply_sid",
            # 物化视图按 PREVIEW_KEYS 剥离预览，应用时从顶层快照回退读取
            "_session_artifacts": {"script_generation": current},
            "pending_review": preview,
        },
        {"selected_issue_ids": ["qr_1"]},
    ))

    payload = result["payload"]
    assert result["revision_applied"] is True
    assert payload["episodes"][0]["content"] == "第一行\n第二行（改）\n第三行"
    assert "pending_review" not in payload
    assert "line_annotations" not in payload and "scene_spans" not in payload
    assert payload["revision_history"][-1]["instruction"].startswith("剧本AI质检：")
    assert payload["review_rollback"]["episodes"][0]["content"] == "第一行\n第二行\n第三行"
    assert current["episodes"][0]["content"] == "第一行\n第二行\n第三行", " 不得改写传入产物"


def test_apply_script_review_stores_rollback_and_restores():
    current = _artifact()
    preview = {
        "summary": "一条建议",
        "base_version": script_revision.content_fingerprint(current),
        "issues": [{
            "id": "qr_1", "episode_number": 1, "line": 2, "type": "dialogue",
            "severity": "high", "problem": "生硬", "action": "replace",
            "original": "第二行", "text": "第二行（改）",
        }],
    }
    applied = asyncio.run(ScriptWriterAgent()._run_apply_script_review(
        {
            "session_id": "rollback_sid",
            "_session_artifacts": {"script_generation": current},
            "pending_review": preview,
        },
        {"selected_issue_ids": ["qr_1"]},
    ))["payload"]

    assert applied["episodes"][0]["content"] == "第一行\n第二行（改）\n第三行"
    assert applied["review_rollback"]["episodes"][0]["content"] == "第一行\n第二行\n第三行"

    restored = asyncio.run(ScriptWriterAgent()._run_rollback_script_review(
        {
            "session_id": "rollback_sid",
            "_session_artifacts": {"script_generation": applied},
        },
        {"action": "rollback_script_review"},
    ))
    payload = restored["payload"]
    assert restored["revision_applied"] is True
    assert payload["episodes"][0]["content"] == "第一行\n第二行\n第三行"
    assert "review_rollback" not in payload
    assert "pending_review" not in payload


def test_rollback_script_review_rejects_after_further_edit():
    current = _artifact()
    preview = {
        "summary": "一条建议",
        "base_version": script_revision.content_fingerprint(current),
        "issues": [{
            "id": "qr_1", "episode_number": 1, "line": 2, "problem": "生硬",
            "action": "replace", "original": "第二行", "text": "第二行（改）",
        }],
    }
    applied = asyncio.run(ScriptWriterAgent()._run_apply_script_review(
        {
            "session_id": "rollback_sid",
            "_session_artifacts": {"script_generation": current},
            "pending_review": preview,
        },
        {"selected_issue_ids": ["qr_1"]},
    ))["payload"]
    applied["episodes"][0]["content"] += "\n额外修改"
    with pytest.raises(ValueError, match="无法自动撤销"):
        asyncio.run(ScriptWriterAgent()._run_rollback_script_review(
            {
                "session_id": "rollback_sid",
                "_session_artifacts": {"script_generation": applied},
            },
            {"action": "rollback_script_review"},
        ))


def test_normalize_review_allows_dialogue_rewrite_within_length():
    episodes = [{
        "episode_number": 1,
        "content": "小刘（温和）：您好，请坐，请问需要办理什么业务？",
    }]
    text = "小刘（温和）：您好，请坐，请问今天需要办理哪项业务？"
    raw = {
        "summary": "较大润色",
        "issues": [{
            "episode_number": 1, "line": 1, "problem": "较大润色",
            "action": "replace", "text": text,
        }],
    }
    normalized = script_review.normalize_review(raw, episodes)
    assert normalized is not None
    assert [item["text"] for item in normalized["issues"]] == [text]


def test_rollback_allows_derived_field_only_change():
    current = _artifact()
    preview = {
        "summary": "一条建议",
        "base_version": script_revision.content_fingerprint(current),
        "issues": [{
            "id": "qr_1", "episode_number": 1, "line": 2, "problem": "生硬",
            "action": "replace", "original": "第二行", "text": "第二行（改）",
        }],
    }
    applied = asyncio.run(ScriptWriterAgent()._run_apply_script_review(
        {
            "session_id": "rollback_sid",
            "_session_artifacts": {"script_generation": current},
            "pending_review": preview,
        },
        {"selected_issue_ids": ["qr_1"]},
    ))["payload"]
    # 服务端回写派生字段不应让撤销失效
    applied["episodes"][0]["line_start"] = 1
    applied["episodes"][0]["line_end"] = 3
    applied["line_annotations"] = [{"line": 0, "type": "action"}]
    applied["scene_spans"] = [{"line": 0, "setting": "办公室"}]
    applied["import_review"] = {"stale": True}

    restored = asyncio.run(ScriptWriterAgent()._run_rollback_script_review(
        {
            "session_id": "rollback_sid",
            "_session_artifacts": {"script_generation": applied},
        },
        {"action": "rollback_script_review"},
    ))["payload"]
    assert restored["episodes"][0]["content"] == "第一行\n第二行\n第三行"
    assert "review_rollback" not in restored


def test_rollback_rejects_auxiliary_change():
    current = _artifact()
    preview = {
        "summary": "一条建议",
        "base_version": script_revision.content_fingerprint(current),
        "issues": [{
            "id": "qr_1", "episode_number": 1, "line": 2, "problem": "生硬",
            "action": "replace", "original": "第二行", "text": "第二行（改）",
        }],
    }
    applied = asyncio.run(ScriptWriterAgent()._run_apply_script_review(
        {
            "session_id": "rollback_sid",
            "_session_artifacts": {"script_generation": current},
            "pending_review": preview,
        },
        {"selected_issue_ids": ["qr_1"]},
    ))["payload"]
    applied["summary"] = "用户后改的摘要"
    with pytest.raises(ValueError, match="无法自动撤销"):
        asyncio.run(ScriptWriterAgent()._run_rollback_script_review(
            {
                "session_id": "rollback_sid",
                "_session_artifacts": {"script_generation": applied},
            },
            {"action": "rollback_script_review"},
        ))


def test_apply_script_review_requires_selection_and_valid_base():
    current = _artifact()
    base = {
        "session_id": "apply_sid",
        "_session_artifacts": {"script_generation": current},
        "pending_review": {
            "summary": "x",
            "base_version": script_revision.content_fingerprint(current),
            "issues": [],
        },
    }
    with pytest.raises(ValueError, match="至少选择"):
        asyncio.run(ScriptWriterAgent()._run_apply_script_review(base, {"selected_issue_ids": []}))

    stale = dict(base)
    stale["pending_review"] = {**base["pending_review"], "base_version": "stale"}
    with pytest.raises(ValueError, match="过期"):
        asyncio.run(
            ScriptWriterAgent()._run_apply_script_review(
                stale, {"selected_issue_ids": ["qr_1"]}
            )
        )


def test_review_script_rejects_generated_session(monkeypatch):
    """灵感生成会话即使直接调 API 也不得触发质检（优化点 3.1）。"""
    current = _artifact()
    current["meta"] = {"language": "zh"}
    with pytest.raises(ValueError, match="仅支持导入剧本"):
        asyncio.run(ScriptWriterAgent()._run_review_script(
            {
                "session_id": "idea_sid",
                "llm_model": "fake-model",
                "_session_meta": {"language": "zh"},
                "_session_artifacts": {"script_generation": current},
            },
            {"action": "review_script"},
        ))


def test_apply_script_review_rejects_generated_session():
    current = _artifact()
    current["meta"] = {"language": "zh"}
    with pytest.raises(ValueError, match="仅支持导入剧本"):
        asyncio.run(ScriptWriterAgent()._run_apply_script_review(
            {
                "session_id": "idea_sid",
                "_session_artifacts": {"script_generation": current},
                "pending_review": {
                    "summary": "x",
                    "base_version": script_revision.content_fingerprint(current),
                    "issues": [],
                },
            },
            {"selected_issue_ids": ["qr_1"]},
        ))


def test_llm_query_passes_review_timeout(monkeypatch):
    """质检长输出需要把 600s 显式传给自定义模型客户端，其它调用保持默认。"""
    import models.custom_llm as custom_llm
    import models.llm_client as llm_client

    seen = {}

    class FakeClient:
        def __init__(self, meta, timeout=300.0):
            seen["timeout"] = timeout

        def query(self, *args, **kwargs):
            return "ok"

        def close(self):
            pass

    monkeypatch.setattr(custom_llm, "CustomChatClient", FakeClient)
    monkeypatch.setattr(
        llm_client, "resolve_model_entry",
        lambda model: ("custom", {"id": model, "request_model": model, "base_url": "http://x"}),
    )
    assert llm_client.LLM().query("hi", model="fake-custom", timeout=600.0) == "ok"
    assert seen["timeout"] == 600.0
    assert llm_client.LLM().query("hi", model="fake-custom") == "ok"
    assert seen["timeout"] == 300.0


def test_review_prompt_templates_reachable_in_both_languages():
    for lang in ("zh", "en"):
        template = load_prompt("script", "review_script", lang)
        assert "{episodes}" in template
        assert "{characters}" in template
        assert "{settings}" in template
        assert "replace" in template and "insert_after" in template and "delete" in template
