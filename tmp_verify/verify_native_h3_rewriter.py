# 任务 3.1/3.2/4.1-4.4 验证：resolver 契约、校验器规则、主循环重试、continuity、角色注入、grounding 三路径、配置
import asyncio
import copy
import os
import sys

sys.path.insert(0, "/app")
os.chdir("/app")

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        failures.append(name)


import config
from core.agents.prompt_rewrite_agent import (
    PromptRewriteAgent, resolve_h3_mode, _validate_h3_prompt, _continuity_from_prompt, _extract_prompt,
)

# ═══ 1. mode resolver 契约（决策文档 §5.2 表） ═══
check("1a reference 模式→Ref2VA", resolve_h3_mode("reference") == "Ref2VA")
check("1b 首帧→I2VA", resolve_h3_mode("first_frame", has_first_frame=True) == "I2VA")
check("1c 首尾帧→FL2VA", resolve_h3_mode("start_end_frame", has_first_frame=True, has_last_frame=True) == "FL2VA")
check("1d 仅尾帧→L2VA", resolve_h3_mode("start_end_frame", has_last_frame=True) == "L2VA")
check("1e start_end_frame 缺尾帧回退 I2VA（与传输层回退一致）",
      resolve_h3_mode("start_end_frame", has_first_frame=True, has_last_frame=False) == "I2VA")
check("1f 无图→T2VA", resolve_h3_mode("first_frame") == "T2VA")
check("1g reference 优先于帧", resolve_h3_mode("reference", has_first_frame=True) == "Ref2VA")
# 传输层归类契约（_task_for_input 逻辑映射，代码已核实）
TRANSPORT = {"T2VA": "t2va", "I2VA": "fl2va", "L2VA": "fl2va", "FL2VA": "fl2va", "Ref2VA": "ref2va"}
check("1h 两级契约映射完备", set(TRANSPORT) == {"T2VA", "I2VA", "L2VA", "FL2VA", "Ref2VA"})

# ═══ 2. 校验器规则 ═══
GOOD_BASE = (
    "integrated_multimodal_description: [Shot 1] A grey tabby cat walks through a rainy neon alley for 8 seconds, "
    "low-angle medium shot, wet asphalt reflecting pink signs.\n"
    "overall_soundscape: Steady rain hits the asphalt, distant traffic hum.\n"
    "non_diegetic_music: None."
)
check("2a 合法基础样本通过", _validate_h3_prompt(GOOD_BASE, "I2VA", 8) == [])
check("2b 漏字段捕获", any("overall_soundscape" in v for v in _validate_h3_prompt(
    "integrated_multimodal_description: cat for 8 seconds.\nnon_diegetic_music: None.", "T2VA", 8)))
check("2c 字段乱序捕获", any("顺序" in v for v in _validate_h3_prompt(
    "overall_soundscape: rain.\nintegrated_multimodal_description: cat for 8 seconds.\nnon_diegetic_music: None.",
    "T2VA", 8)))
check("2d 时长不一致捕获", any("时长" in v for v in _validate_h3_prompt(GOOD_BASE, "T2VA", 12)))
check("2e 抽象词捕获", any("抽象词" in v and "cinematic" in v for v in _validate_h3_prompt(
    GOOD_BASE.replace("low-angle medium shot", "cinematic shot"), "T2VA", 8)))
check("2f 中文正文捕获", any("英文" in v for v in _validate_h3_prompt(GOOD_BASE + "这是一段中文正文" * 30, "T2VA", 8)))
check("2g 引号内中文豁免", _validate_h3_prompt(
    GOOD_BASE + '\nThe cat passes a sign reading "猫の店".', "T2VA", 8) == [])
REF_TEXT = (
    "subject_definitions: <Picture 1> a grey tabby cat with amber eyes.\n"
    "summary: 8 seconds of the cat walking.\n"
    "retention_analysis: <Picture 1> appears from 0.0s to 8.0s.\n"
    "detailed_description: The cat walks for 8 seconds while rain falls.\n"
    "overall_soundscape: rain.\nnon_diegetic_music: None."
)
check("2h 合法 Ref2VA 通过", _validate_h3_prompt(REF_TEXT, "Ref2VA", 8) == [])
check("2i 未定义标签捕获", any("未定义" in v for v in _validate_h3_prompt(
    REF_TEXT.replace("retention_analysis: <Picture 1>", "retention_analysis: <Picture 2>"), "Ref2VA", 8)))
check("2j 空结果捕获", _validate_h3_prompt("", "T2VA", 8) == ["改写结果为空"])
check("2k continuity 提取", "tabby" in _continuity_from_prompt(GOOD_BASE, "I2VA").lower())

# ═══ 3. 主循环：stub LLM ═══
config.Config.H3_REWRITE_ENABLED = True
config.Config.H3_REWRITE_LLM_MODEL = ""
config.Config.H3_REWRITE_VLM_MODEL = ""
config.Config.H3_REWRITE_GROUNDING_ENABLED = True

STORYBOARD = {"episodes": [{"episode_number": 1, "segments": [
    {"segment_id": "seg_01_01", "total_duration": 8, "characters": ["林夏"],
     "setting": "雨夜小巷", "shots": [{"content": "林夏走在雨夜小巷", "duration": 8}]},
    {"segment_id": "seg_01_02", "total_duration": 8, "characters": ["林夏"],
     "shots": [{"content": "林夏抬头看霓虹灯", "duration": 8}]},
]}]}
CHARACTERS = {"characters": [{"name": "林夏", "description": "短发女孩，穿黑色风衣，左眉有疤"}]}


def make_input(artifacts_extra=None, video_mode="first_frame"):
    arts = {"storyboard": copy.deepcopy(STORYBOARD), "character_design": CHARACTERS}
    arts.update(artifacts_extra or {})
    return {"session_id": "s1", "llm_model": "stub-llm", "vlm_model": "stub-vlm",
            "video_generation_mode": video_mode,
            "_session_artifacts": arts, "_session_meta": {}}


class FakeLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def query(self, prompt, image_urls=[], model="", safe_content=True, task_id=None, web_search=False):
        self.prompts.append(prompt)
        return self.outputs.pop(0) if self.outputs else self.outputs[-1] if self.outputs else ""


GOOD_LLM_OUT = f"模式判定：I2VA（首帧对齐）\n```\n{GOOD_BASE}\n```"
BAD_LLM_OUT = "```\nintegrated_multimodal_description: cat walks.\n```"  # 漏两字段 + 无时长

# 3a 正常路径单次调用 + continuity/grounding 无图场景（first_frame 无 selected → T2VA）
agent = PromptRewriteAgent()
agent.set_progress_callback(lambda *a, **k: None)
agent._llm = FakeLLM([GOOD_LLM_OUT.replace("I2VA", "T2VA"), GOOD_LLM_OUT.replace("I2VA", "T2VA")])
result = asyncio.run(agent.process(make_input()))
items = result["payload"]["items"]
llm_calls = len(agent._llm.prompts)
check("3a1 全部条目 done", all(i["status"] == "done" for i in items), str([(i['id'], i['status'], i.get('error')) for i in items]))
check("3a2 正常路径每条 1 次调用", llm_calls == 2, f"calls={llm_calls}")
check("3a3 无图模式 T2VA", all(i["input_mode"] == "T2VA" for i in items))
check("3a4 payload 无外部会话键", "session_id" not in result["payload"])
check("3a5 continuity 字段落盘", all("continuity" in i for i in items))
check("3a6 停点标记", result.get("requires_intervention") is True and result.get("stage_completed") is True)

# 3b 校验失败回喂重试后成功（seg1 第一次坏输出→回喂重试成功；seg2 正常 1 次）
agent2 = PromptRewriteAgent()
agent2.set_progress_callback(lambda *a, **k: None)
agent2._llm = FakeLLM([BAD_LLM_OUT, GOOD_LLM_OUT, GOOD_LLM_OUT])
result2 = asyncio.run(agent2.process(make_input()))
item2 = result2["payload"]["items"][0]
check("3b1 重试后成功", item2["status"] == "done", item2.get("error", ""))
check("3b2 seg1 共 2 次 + seg2 1 次 = 3 次调用", len(agent2._llm.prompts) == 3, f"calls={len(agent2._llm.prompts)}")
check("3b3 回喂含违规清单", "违规清单" in agent2._llm.prompts[1] and "overall_soundscape" in agent2._llm.prompts[1])

# 3c 重试耗尽 → failed 带违规清单（每分镜 1+2 次调用）
agent3 = PromptRewriteAgent()
agent3.set_progress_callback(lambda *a, **k: None)
agent3._llm = FakeLLM([BAD_LLM_OUT] * 8)
result3 = asyncio.run(agent3.process(make_input()))
item3 = result3["payload"]["items"][0]
check("3c1 重试耗尽 failed", item3["status"] == "failed")
check("3c2 违规清单入 error", "结构校验未通过" in item3.get("error", "") and "overall_soundscape" in item3.get("error", ""))
check("3c3 每分镜恰 3 次调用（1+2）", len(agent3._llm.prompts) == 6, f"calls={len(agent3._llm.prompts)}")

# ═══ 4. 角色注入 + continuity 链 + 五级优先级 ═══
agent4 = PromptRewriteAgent()
agent4.set_progress_callback(lambda *a, **k: None)
agent4._llm = FakeLLM([GOOD_LLM_OUT.replace("I2VA", "T2VA")] * 2)
asyncio.run(agent4.process(make_input()))
p1, p2 = agent4._llm.prompts
check("4a 角色权威事实注入", "林夏" in p1 and "黑色风衣" in p1)
check("4b 场景设定注入", "雨夜小巷" in p1)
check("4c 分镜2 prompt 含分镜1 continuity", "tabby" in p2.lower() or "cat" in p2.lower())
check("4d continuity 标注辅助信息", "辅助信息" in p2 and "非事实来源" in p2)

# ═══ 5. VLM grounding 三路径 ═══
# 造真实图片文件（hash 缓存需要读文件）
os.makedirs("/tmp/h3g", exist_ok=True)
IMG1 = "/tmp/h3g/ref1.png"
with open(IMG1, "wb") as f:
    f.write(b"\x89PNG-fake-content-1")
IMG2 = "/tmp/h3g/ref2.png"
with open(IMG2, "wb") as f:
    f.write(b"\x89PNG-fake-content-2")

REF_ART = {"reference_generation": {"scenes": [
    {"id": "seg_01_01", "selected": IMG1, "status": "done"},
    {"id": "seg_01_02", "selected": IMG2, "status": "done"},
]}}


class FakeVLM:
    def __init__(self):
        self.calls = 0

    def query(self, prompt, image_paths=None, model="", session_id=None):
        self.calls += 1
        return "画面中一只灰色虎斑猫站在雨中的巷子里"


# 5a grounding 注入（first_frame + selected → I2VA）
agent5 = PromptRewriteAgent()
agent5.set_progress_callback(lambda *a, **k: None)
agent5._llm = FakeLLM([GOOD_LLM_OUT, GOOD_LLM_OUT])
agent5._vlm = FakeVLM()
result5 = asyncio.run(agent5.process(make_input(REF_ART)))
items5 = result5["payload"]["items"]
check("5a1 模式 I2VA（有选中首帧图）", items5[0]["input_mode"] == "I2VA")
check("5a2 VLM 描述进 prompt", "灰色虎斑猫" in agent5._llm.prompts[0])
check("5a3 grounding 元数据落盘", items5[0].get("grounding", {}).get("described") is True
      and items5[0]["grounding"].get("image_hash"))
check("5a4 VLM 调用 2 次（每分镜一图）", agent5._vlm.calls == 2, f"calls={agent5._vlm.calls}")

# 5b 同图重生成命中缓存（regenerate seg_01_01，图未变 → 零 VLM 调用）
agent5._llm = FakeLLM([GOOD_LLM_OUT])
prev = {"prompt_rewrite": {"items": copy.deepcopy(items5)}}
result5b = asyncio.run(agent5.process(make_input(REF_ART), intervention={"regenerate_items": ["seg_01_01"]}))
check("5b1 重生成只返回目标条目", [i["id"] for i in result5b["payload"]["items"]] == ["seg_01_01"])
check("5b2 缓存命中零 VLM 调用", agent5._vlm.calls == 2, f"calls={agent5._vlm.calls}")

# 5c VLM 失败回退：模式不变 + text_only +（M-d）阶段完成汇总降级日志
class BoomVLM:
    def query(self, *a, **k):
        raise RuntimeError("vlm down")


import logging as _logging
_captured: list = []


class _CapHandler(_logging.Handler):
    def emit(self, record):
        _captured.append(record.getMessage())


_agent_logger = _logging.getLogger("core.agents.prompt_rewrite_agent")
_agent_logger.setLevel(_logging.DEBUG)  # 脚本环境根 logger 默认 WARNING，不设级别会过滤掉 INFO 汇总（容器内应用自身已配 INFO）
_handler = _CapHandler()
_agent_logger.addHandler(_handler)
try:
    agent5c = PromptRewriteAgent()
    agent5c.set_progress_callback(lambda *a, **k: None)
    agent5c._llm = FakeLLM([GOOD_LLM_OUT, GOOD_LLM_OUT])
    agent5c._vlm = BoomVLM()
    result5c = asyncio.run(agent5c.process(make_input(REF_ART)))
    items5c = result5c["payload"]["items"]
    item5c = items5c[0]
    check("5c1 VLM 失败不阻塞（改写成功）", item5c["status"] == "done", item5c.get("error", ""))
    check("5c2 模式保持 I2VA 不降级", item5c["input_mode"] == "I2VA")
    check("5c3 text_only 标记", item5c.get("grounding", {}).get("text_only") is True)
    check("5c4 全分镜降级标记（M-d）", all(i.get("grounding", {}).get("text_only") for i in items5c),
          str([i.get("grounding") for i in items5c])[:120])
    check("5c5 阶段完成汇总降级日志（M-d）",
          any("grounding 降级 2/2" in m for m in _captured), str(_captured)[-160:])
finally:
    _agent_logger.removeHandler(_handler)

# 5d grounding 开关关闭 → 零 VLM 调用、模式不变
config.Config.H3_REWRITE_GROUNDING_ENABLED = False
agent5d = PromptRewriteAgent()
agent5d.set_progress_callback(lambda *a, **k: None)
agent5d._llm = FakeLLM([GOOD_LLM_OUT])
agent5d._vlm = FakeVLM()
result5d = asyncio.run(agent5d.process(make_input(REF_ART)))
check("5d1 开关关闭零 VLM 调用", agent5d._vlm.calls == 0)
check("5d2 模式仍 I2VA", result5d["payload"]["items"][0]["input_mode"] == "I2VA")
config.Config.H3_REWRITE_GROUNDING_ENABLED = True

# 5e 接线级（C-1 回归）：真实会话配置 start_end_frame 经 process() 入口必须产出 FL2VA
# （尾帧 = 下一分镜选中图；历史缺陷：接线层误用非规范值 "start_end" 致 FL2VA/L2VA 永不触发，纯函数单测无法拦截）
agent5e = PromptRewriteAgent()
agent5e.set_progress_callback(lambda *a, **k: None)
agent5e._llm = FakeLLM([GOOD_LLM_OUT, GOOD_LLM_OUT])
agent5e._vlm = FakeVLM()
result5e = asyncio.run(agent5e.process(make_input(REF_ART, video_mode="start_end_frame")))
items5e = result5e["payload"]["items"]
check("5e1 start_end_frame 接线产出 FL2VA（C-1）", items5e[0]["input_mode"] == "FL2VA",
      f"got {items5e[0]['input_mode']}")
check("5e2 末分镜无下一选中图 → I2VA", items5e[1]["input_mode"] == "I2VA",
      f"got {items5e[1]['input_mode']}")

# 5f 非规范值 "start_end" 不属于生产输入空间：接线层不触发尾帧（与规范判定互斥，防回归到旧字面量）
agent5f = PromptRewriteAgent()
agent5f.set_progress_callback(lambda *a, **k: None)
agent5f._llm = FakeLLM([GOOD_LLM_OUT, GOOD_LLM_OUT])
agent5f._vlm = FakeVLM()
result5f = asyncio.run(agent5f.process(make_input(REF_ART, video_mode="start_end")))
check("5f 非规范值不触发 FL2VA（仅规范值生效）", result5f["payload"]["items"][0]["input_mode"] == "I2VA",
      f"got {result5f['payload']['items'][0]['input_mode']}")

# ═══ 6. 修订流程（用户修改意见进「用户修改」区） ═══
agent6 = PromptRewriteAgent()
agent6.set_progress_callback(lambda *a, **k: None)
agent6._llm = FakeLLM([GOOD_LLM_OUT])
agent6._vlm = FakeVLM()
prev_items6 = [{"id": "seg_01_01", "status": "done", "rewritten_prompt": "OLD VERSION",
                "versions": [], "duration": 8, "input_mode": "I2VA", "name": "第1集-片段1", "index": 1}]
result6 = asyncio.run(agent6.process(
    make_input({**REF_ART, "prompt_rewrite": {"items": prev_items6}}),
    intervention={"revise_items": [{"id": "seg_01_01", "instruction": "改成雨天"}]},
))
item6 = result6["payload"]["items"][0]
check("6a 修订成功", item6["status"] == "done")
check("6b 修改意见进 prompt", "改成雨天" in agent6._llm.prompts[0])
check("6c 上一版携带", "OLD VERSION" in agent6._llm.prompts[0])
check("6d 旧版入 versions(superseded)", any(v.get("source") == "superseded" for v in item6.get("versions", [])))

# ═══ 7. 禁用态与配置 ═══
config.Config.H3_REWRITE_ENABLED = False
try:
    asyncio.run(PromptRewriteAgent().process(make_input()))
    check("7a 禁用态明确拒绝", False)
except ValueError as exc:
    check("7a 禁用态明确拒绝", "VC_H3_REWRITE__ENABLE" in str(exc), str(exc)[:60])
config.Config.H3_REWRITE_ENABLED = True

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
