"""任务 4.1 验证脚本：剧本阶段片子类型判定与三类宣传片导向接线（容器内运行）。

  docker exec -i -w /app -e PYTHONPATH=/app video-claw-backend /app/.venv/bin/python - \
    < tmp_verify/verify_script_film_type.py

覆盖（openspec change: script-stage-promo-type-rework）：
  - 二维判定调用（片子类型 × 主体形式）、默认回退、异常中断防护、白名单归一化（含 en 同义词）
  - 改写模板 .format(..., film_type=..., subject_mode=...) 无 KeyError（zh/en）
  - meta.film_type / meta.subject_mode 写入、meta_extract 交叉校验不覆盖判定值
  - 续写沿用原类型与主体形式（读产物 meta，缺省默认）
  - modified_script 干预路径缺省回填
  - 非人物主体兼容（主体可为银行/产品/物体/地点/logo，禁止拟人化对白，
    非人物主体必须提取为角色，场景=背景地点图片、人物行含非人物主体）
  - 人物故事 / 旁白描述两种主体形式端到端
"""
import asyncio
import json
import sys

sys.dont_write_bytecode = True

import models.llm_client as llm_module
from core.agents.script_agent import (
    ScriptWriterAgent,
    FILM_TYPES,
    DEFAULT_FILM_TYPE,
    SUBJECT_MODES,
    DEFAULT_SUBJECT_MODE,
    _FILM_TYPE_ALIASES,
    _SUBJECT_MODE_ALIASES,
)
from prompts.loader import load_prompt_with_fallback

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


# ── mock LLM：按 prompt 内容路由返回 canned 响应 ────────────────────────────
PROMPTS_SEEN = []
CLASSIFY_RESPONSE = '{"film_type": "营销", "subject_mode": "旁白描述"}'
META_EXTRACT_FILM_TYPE = "营销"
META_EXTRACT_SUBJECT_MODE = "旁白描述"
QUERY_ERROR_MARKERS = set()


def fake_script(episodes=4):
    """结构良好的 4 集剧本：每集 26 个计举行词（25-30 区间，不触发 trim/repair）。"""
    lines = []
    for ep in range(1, episodes + 1):
        lines.append(f"## 第{ep}集")
        lines.append(f"**第{ep}集-第1场 日 内 展厅**")
        lines.append("人物：主播, 客户")
        for i in range(26):
            lines.append(f"主播（微笑）：这是第{ep}集第{i + 1}句产品介绍台词。")
    return "\n".join(lines) + "\n"


def fake_meta():
    return json.dumps(
        {
            "title": "智能水杯全解析",
            "logline": "一分钟看懂智能水杯",
            "genre": ["产品介绍"],
            "mood": "明快",
            "film_type": META_EXTRACT_FILM_TYPE,
            "subject_mode": META_EXTRACT_SUBJECT_MODE,
            "characters": [
                {"name": "主播", "description": "青年女性，马尾，白色衬衫，身形匀称", "species": "人类"}
            ],
            "settings": [
                {"name": "展厅", "description": "现代简约展厅，充足自然光，木质展台与金属展架"}
            ],
        },
        ensure_ascii=False,
    )


def fake_episodes():
    return json.dumps(
        [
            {"episode_number": n, "act_title": f"第{n}集", "content": f"第{n}集正文"}
            for n in range(1, 5)
        ],
        ensure_ascii=False,
    )


class MockLLM:
    def __init__(self, *args, **kwargs):
        pass

    def query(self, prompt, image_urls=None, model=None, safe_content=True,
              task_id=None, web_search=False):
        PROMPTS_SEEN.append(prompt)
        for marker in QUERY_ERROR_MARKERS:
            if marker in prompt:
                raise RuntimeError(f"mock LLM failure on: {marker}")
        if "影片类型判定专家" in prompt or "film classification expert" in prompt:
            return CLASSIFY_RESPONSE
        if "台词评估" in prompt or "dialogue evaluation expert" in prompt:
            return "无修改意见"
        if "情节评估" in prompt or "plot evaluation expert" in prompt:
            return "无修改意见"
        if "剧本设计师" in prompt or "script designer" in prompt:
            return fake_meta()
        if "剧本分析师" in prompt or "script analyst" in prompt:
            return fake_episodes()
        if "影视统筹助理" in prompt:
            return json.dumps({"new_characters": [], "new_settings": []}, ensure_ascii=False)
        if "续写灵感" in prompt:
            return "继续介绍智能水杯的续航能力"
        # generate_script / revise_script / trim / repair / smart_continue 均返回结构良好剧本
        return fake_script()


llm_module.LLM = MockLLM
# 避免验证脚本写落盘产物
ScriptWriterAgent._save_result = lambda self, json_data, sid, is_zh: None


def run_agent(input_data, intervention=None):
    agent = ScriptWriterAgent()
    return asyncio.run(agent.process(input_data, intervention=intervention))


def base_input(idea="介绍一款可以语音控温的智能水杯"):
    return {
        "session_id": "verify-film-type",
        "idea": idea,
        "style": "anime",
        "llm_model": "mock-llm",
        "episodes": 4,
        "web_search": False,
    }


# ── A. 主流程：中文灵感判定为营销 + 旁白描述 ────────────────────────────────
CLASSIFY_RESPONSE = '{"film_type": "营销", "subject_mode": "旁白描述"}'
META_EXTRACT_FILM_TYPE = "营销"
META_EXTRACT_SUBJECT_MODE = "旁白描述"
PROMPTS_SEEN.clear()
result = run_agent(base_input())
payload = result["payload"]

check("A1 二维判定写入 meta（film_type/subject_mode）",
      payload["meta"]["film_type"] == "营销" and payload["meta"]["subject_mode"] == "旁白描述",
      f"got {payload['meta'].get('film_type')!r}/{payload['meta'].get('subject_mode')!r}")
check("A2 判定提示词携带 idea",
      any("影片类型判定专家" in p and "智能水杯" in p for p in PROMPTS_SEEN))
check("A3 generate_script prompt 注入 film_type + subject_mode",
      any("顶级宣传片编剧专家" in p and "营销" in p and "旁白描述" in p for p in PROMPTS_SEEN))
check("A4 eval_dialogue/eval_plot/revise prompt 注入 film_type + subject_mode",
      any("台词评估" in p and "营销" in p and "旁白描述" in p for p in PROMPTS_SEEN)
      and any("情节评估" in p and "营销" in p and "旁白描述" in p for p in PROMPTS_SEEN)
      and any("请根据意见优化剧本" in p and "营销" in p and "旁白描述" in p for p in PROMPTS_SEEN))
check("A5 分集结构完整（episode_number 1..4 连续）",
      [e["episode_number"] for e in payload["episodes"]] == [1, 2, 3, 4])
check("A6 产物既有 schema 字段不变",
      all(k in payload for k in ("title", "logline", "genre", "mood", "characters", "settings", "episodes")))

# ── B. 判定输出不可识别 → 两个维度均回退默认 ────────────────────────────────
CLASSIFY_RESPONSE = "完全无法识别的内容"
PROMPTS_SEEN.clear()
payload = run_agent(base_input())["payload"]
check("B1 不可识别判定输出回退默认（介绍/旁白描述）",
      payload["meta"]["film_type"] == "介绍" and payload["meta"]["subject_mode"] == "旁白描述",
      f"got {payload['meta']['film_type']!r}/{payload['meta']['subject_mode']!r}")

# ── C. 英文灵感 + 英文同义词 → 归一化为中文规范名 ───────────────────────────
CLASSIFY_RESPONSE = '{"film_type": "promo", "subject_mode": "voiceover"}'
PROMPTS_SEEN.clear()
payload = run_agent(base_input(idea="A voice-controlled smart bottle promo"))["payload"]
check("C1 en 路径使用英文判定模板",
      any("film classification expert" in p for p in PROMPTS_SEEN))
check("C2 英文同义词归一化（promo→宣传，voiceover→旁白描述）",
      payload["meta"]["film_type"] == "宣传" and payload["meta"]["subject_mode"] == "旁白描述",
      f"got {payload['meta']['film_type']!r}/{payload['meta']['subject_mode']!r}")

# ── D. 判定调用异常 → 回退默认，流程不中断 ─────────────────────────────────
CLASSIFY_RESPONSE = '{"film_type": "营销", "subject_mode": "旁白描述"}'
QUERY_ERROR_MARKERS.add("影片类型判定专家")
PROMPTS_SEEN.clear()
payload = run_agent(base_input())["payload"]
QUERY_ERROR_MARKERS.clear()
check("D1 判定调用异常回退默认", payload["meta"]["film_type"] == "介绍"
      and payload["meta"]["subject_mode"] == "旁白描述",
      f"got {payload['meta']['film_type']!r}/{payload['meta']['subject_mode']!r}")
check("D2 判定异常后生成流程仍完成", len(payload["episodes"]) == 4)

# ── E. meta_extract 交叉校验：两个维度不一致均不覆盖判定值 ──────────────────
CLASSIFY_RESPONSE = '{"film_type": "营销", "subject_mode": "旁白描述"}'
META_EXTRACT_FILM_TYPE = "介绍"
META_EXTRACT_SUBJECT_MODE = "人物故事"
PROMPTS_SEEN.clear()
payload = run_agent(base_input())["payload"]
META_EXTRACT_FILM_TYPE = "营销"
META_EXTRACT_SUBJECT_MODE = "旁白描述"
check("E1 meta_extract 不一致时判定值不被覆盖",
      payload["meta"]["film_type"] == "营销" and payload["meta"]["subject_mode"] == "旁白描述",
      f"got {payload['meta']['film_type']!r}/{payload['meta']['subject_mode']!r}")

# ── F. modified_script 干预路径缺省回填 ─────────────────────────────────────
res = run_agent(base_input(), intervention={
    "modified_script": {"title": "手改稿", "episodes": [{"episode_number": 1, "content": "x"}]}
})
check("F1 modified_script 缺 meta 时回填默认",
      res["payload"]["meta"]["film_type"] == "介绍"
      and res["payload"]["meta"]["subject_mode"] == "旁白描述",
      f"got {res['payload']['meta'].get('film_type')!r}/{res['payload']['meta'].get('subject_mode')!r}")

res = run_agent(base_input(), intervention={
    "modified_script": json.dumps(
        {"title": "手改稿", "meta": {"film_type": "宣传", "subject_mode": "人物故事"}},
        ensure_ascii=False)
})
check("F2 modified_script 已有值时保留",
      res["payload"]["meta"]["film_type"] == "宣传"
      and res["payload"]["meta"]["subject_mode"] == "人物故事",
      f"got {res['payload']['meta'].get('film_type')!r}/{res['payload']['meta'].get('subject_mode')!r}")

# ── G. 续写沿用原类型与主体形式 ────────────────────────────────────────────
continue_input = {
    "session_id": "verify-film-type-cont",
    "llm_model": "mock-llm",
    "episodes": [
        {"episode_number": 1, "act_title": "第1集", "content": "第一集内容"},
        {"episode_number": 2, "act_title": "第2集", "content": "第二集内容"},
    ],
    "characters": [{"character_id": "char_a", "name": "主播", "description": "青年女性"}],
    "settings": [{"setting_id": "set_a", "name": "展厅", "description": "现代展厅"}],
    "meta": {"film_type": "宣传", "subject_mode": "人物故事"},
}
PROMPTS_SEEN.clear()
res = run_agent(continue_input, intervention={
    "action": "smart_continue", "episodes_to_add": 1, "sequel_idea": "介绍续航能力"
})
cont_prompts = list(PROMPTS_SEEN)
check("G1 续写读取产物 meta 并注入 smart_continue prompt",
      any("希望你往下续写" in p and "宣传" in p and "人物故事" in p for p in cont_prompts))
check("G2 续写路径 eval/revise prompt 注入两个维度",
      any("台词评估" in p and "宣传" in p and "人物故事" in p for p in cont_prompts)
      and any("情节评估" in p and "宣传" in p and "人物故事" in p for p in cont_prompts)
      and any("请根据意见优化剧本" in p and "宣传" in p and "人物故事" in p for p in cont_prompts))
check("G3 续写产物保留原类型与主体形式",
      res["payload"]["meta"]["film_type"] == "宣传"
      and res["payload"]["meta"]["subject_mode"] == "人物故事")

continue_input_no_meta = {k: v for k, v in continue_input.items() if k != "meta"}
PROMPTS_SEEN.clear()
run_agent(continue_input_no_meta, intervention={
    "action": "smart_continue", "episodes_to_add": 1, "sequel_idea": "介绍续航能力"
})
check("G4 续写缺 meta 时回退默认",
      any("希望你往下续写" in p and "介绍" in p and "旁白描述" in p for p in PROMPTS_SEEN))

# ── H. 模板 format 冒烟（zh/en，无 KeyError） ───────────────────────────────
smokes = [
    ("classify_film_type", dict(idea="x")),
    ("generate_script", dict(idea="x", style="anime", episodes=4, film_type="宣传", subject_mode="人物故事")),
    ("eval_dialogue", dict(script_text="x", film_type="宣传", subject_mode="人物故事")),
    ("eval_plot", dict(script_text="x", film_type="宣传", subject_mode="人物故事")),
    ("revise_script", dict(script_text="x", dialogue_critique="d", plot_critique="p",
                           film_type="宣传", subject_mode="人物故事")),
    ("trim_script", dict(script_text="x", min_lines=25, max_lines=30, line_report="r",
                         film_type="宣传", subject_mode="人物故事")),
    ("repair_episode_count", dict(script_text="x", episodes=4, episode_report="r",
                                  min_lines=25, max_lines=30, film_type="宣传", subject_mode="人物故事")),
    ("smart_continue_script", dict(episodes_text="e", chars_text="c", settings_text="s",
                                   episodes_to_add=1, sequel_idea="i", start_episode_num=3,
                                   film_type="宣传", subject_mode="人物故事")),
    ("meta_extract", dict(script_text="x", outline="x")),
    ("act_extract", dict(script_text="x", episodes=4)),
]
for lang in ("zh", "en"):
    ok, failed = True, []
    for name, kw in smokes:
        try:
            load_prompt_with_fallback("script", name, lang, "zh").format(**kw)
        except KeyError as e:
            ok = False
            failed.append(f"{name}:{e}")
    check(f"H-{lang} 全部模板 .format(film_type=..., subject_mode=...) 无 KeyError",
          ok, ", ".join(failed))

# ── I. 归一化与解析单测 ─────────────────────────────────────────────────────
unit_cases = {
    "介绍": "介绍", "宣传": "宣传", "营销": "营销",
    "intro": "介绍", "Introduction": "介绍", "promo": "宣传", "promotion": "宣传",
    "marketing": "营销", "营销。理由：强调转化": "营销",
    "garbage": None, "": None,
}
bad = {k: ScriptWriterAgent._normalize_film_type(k) for k, v in unit_cases.items()
       if ScriptWriterAgent._normalize_film_type(k) != v}
check("I1 _normalize_film_type 白名单归一化", not bad, str(bad))

mode_cases = {
    "人物故事": "人物故事", "旁白描述": "旁白描述", "旁白": "旁白描述",
    "character story": "人物故事", "voiceover": "旁白描述", "voice-over": "旁白描述",
    "人物故事。理由：需要通过人物经历展现": "人物故事",
    "旁白描述。理由：通过人物展示产品": "旁白描述",
    "garbage": None,
}
bad = {k: ScriptWriterAgent._normalize_subject_mode(k) for k, v in mode_cases.items()
       if ScriptWriterAgent._normalize_subject_mode(k) != v}
check("I2 _normalize_subject_mode 白名单归一化（含理由句防误命中）", not bad, str(bad))

parse_cases = {
    '{"film_type": "介绍", "subject_mode": "旁白描述"}': ("介绍", "旁白描述"),
    '{"film_type": "promo", "subject_mode": "character story"}': ("宣传", "人物故事"),
    '{"类型": "营销", "形式": "旁白描述"}': ("营销", "旁白描述"),
    "介绍；旁白描述": ("介绍", "旁白描述"),
    "宣传\n人物故事": ("宣传", "人物故事"),
    "完全无法识别": (None, None),
}
bad = {k: ScriptWriterAgent._parse_classification(k) for k, v in parse_cases.items()
       if ScriptWriterAgent._parse_classification(k) != v}
check("I3 _parse_classification 二维解析（JSON/裸文本/失败）", not bad, str(bad))

# ── J. 非人物主体与格式提示约束 ─────────────────────────────────────────────
cls_zh = load_prompt_with_fallback("script", "classify_film_type", "zh", "zh")
cls_en = load_prompt_with_fallback("script", "classify_film_type", "en", "zh")
check("J1 classify 提示词为二维判定且明示主体可为非人物实体",
      all(k in cls_zh for k in ("维度一", "维度二", "人物故事", "旁白描述", "银行", "产品", "logo"))
      and all(k in cls_en.lower() for k in ("dimension 1", "dimension 2", "character story",
                                            "voiceover", "bank", "product", "logo")))

gen_zh = load_prompt_with_fallback("script", "generate_script", "zh", "zh")
gen_en = load_prompt_with_fallback("script", "generate_script", "en", "zh")
check("J2 generate 提示词含主体形式约束 + 场景/角色提示（优化3）",
      all(k in gen_zh for k in ("主体形式", "{subject_mode}", "背景地点图片",
                                "所有的人物以及非人物主体"))
      and all(k in gen_en for k in ("Subject Mode", "{subject_mode}",
                                    "background location image",
                                    "ALL people AND non-person subjects")))

sc_zh = load_prompt_with_fallback("script", "smart_continue_script", "zh", "zh")
check("J3 smart_continue 提示词含主体形式与格式提示",
      all(k in sc_zh for k in ("主体形式", "{subject_mode}", "背景地点图片",
                               "所有的人物以及非人物主体")))

meta_zh = load_prompt_with_fallback("script", "meta_extract", "zh", "zh")
meta_en = load_prompt_with_fallback("script", "meta_extract", "en", "zh")
check("J4 meta_extract 必须提取非人物主体为角色 + subject_mode 字段",
      all(k in meta_zh for k in ("以及剧本的主体对象", "非人物主体", "subject_mode"))
      and all(k in meta_en for k in ("subject object", "Non-person subjects", "subject_mode")))

ed_zh = load_prompt_with_fallback("script", "eval_dialogue", "zh", "zh")
ed_en = load_prompt_with_fallback("script", "eval_dialogue", "en", "zh")
check("J5 eval_dialogue 含拟人化对白检查项",
      "拟人化对白" in ed_zh and "personif" in ed_en.lower())

check("J7 旁白/画外音不作为角色（meta_extract 排除 + generate 人物行不列入）",
      all(k in meta_zh for k in ("旁白", "配音", "不生成肖像"))
      and all(k in meta_en for k in ("voice-over", "dubbing only", "MUST NOT be extracted as a Character"))
      and ("旁白" in gen_zh and "不列入" in gen_zh)
      and ("dubbing only" in gen_en and "NOT on-screen" in gen_en))

# ── L. 每场人物须与动作一致（已撤销“场景元素”行）─────────────────
check("L1 generate/smart_continue 保留人物一致性且已无场景元素行",
      ("一致性" in gen_zh and "人物" in gen_zh and "场景元素" not in gen_zh)
      and ("Consistency" in gen_en and "Scene elements" not in gen_en)
      and ("一致性" in sc_zh and "场景元素" not in sc_zh))
check("L2 eval_plot 含逐场人物一致性检查标准",
      "人物" in load_prompt_with_fallback("script", "eval_plot", "zh", "zh")
      and "Characters:" in load_prompt_with_fallback("script", "eval_plot", "en", "zh"))
check("L3 revise/trim/repair 已移除场景元素行引用",
      all("场景元素" not in load_prompt_with_fallback("script", n, "zh", "zh")
          for n in ("revise_script", "trim_script", "repair_episode_count")))
check("L4 _is_counted_script_line 仍排除场景元素行（防御守卫保留）",
      ScriptWriterAgent._is_counted_script_line("场景元素：城市天际线,交通银行大楼") is False
      and ScriptWriterAgent._is_counted_script_line("Scene elements: a, b") is False
      and ScriptWriterAgent._is_counted_script_line("<action>航拍城市。") is True)

CLASSIFY_RESPONSE = '{"film_type": "介绍", "subject_mode": "旁白描述"}'
META_EXTRACT_FILM_TYPE = "介绍"
META_EXTRACT_SUBJECT_MODE = "旁白描述"
for label, idea in [
    ("产品", "介绍一款可以语音控温的智能水杯"),
    ("银行", "介绍一家面向年轻人的数字银行"),
    ("地点", "宣传我们新开业的山顶露营基地"),
]:
    PROMPTS_SEEN.clear()
    payload = run_agent(base_input(idea=idea))["payload"]
    check(f"J6 [{label}] 非人物主体端到端跑通（判定/分集/schema）",
          payload["meta"]["film_type"] == "介绍"
          and payload["meta"]["subject_mode"] == "旁白描述"
          and [e["episode_number"] for e in payload["episodes"]] == [1, 2, 3, 4]
          and all(k in payload for k in ("title", "logline", "genre", "mood", "characters", "settings")))

# ── K. 人物故事主体形式端到端 ───────────────────────────────────────────────
CLASSIFY_RESPONSE = '{"film_type": "宣传", "subject_mode": "人物故事"}'
META_EXTRACT_FILM_TYPE = "宣传"
META_EXTRACT_SUBJECT_MODE = "人物故事"
PROMPTS_SEEN.clear()
payload = run_agent(base_input(idea="宣传一位坚持手工制鞋四十年的老匠人"))["payload"]
check("K1 人物故事端到端跑通（判定/分集/schema）",
      payload["meta"]["film_type"] == "宣传"
      and payload["meta"]["subject_mode"] == "人物故事"
      and [e["episode_number"] for e in payload["episodes"]] == [1, 2, 3, 4]
      and all(k in payload for k in ("title", "logline", "genre", "mood", "characters", "settings")))
check("K2 人物故事 prompt 注入主体形式",
      any("顶级宣传片编剧专家" in p and "人物故事" in p for p in PROMPTS_SEEN))

# ── 汇总 ────────────────────────────────────────────────────────────────────
print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
    sys.exit(1)
print("ALL CHECKS PASSED")
