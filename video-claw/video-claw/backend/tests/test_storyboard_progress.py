"""分镜阶段进度条细粒度化：单进度源、增量式权重、心跳推进、运行期整数封顶 95。

对应问题：`docs/designs/优化方向.md` 分镜第 1 条「进度条一直是10%，但是文字会变化」。

零付费门：所有用例都不触网。只把"会调模型"的三步换成确定性实现（结构仍由真实的
`_segment_plan_from_units` / `_fallback_design_segment` 构造），`process()` 与
`_design_episode_storyboard()` 的进度上报代码全部走真实路径。
必须在既有后端容器内执行（隔离库 videoclaw_test + videoclaw-test* 桶）。
"""
from __future__ import annotations

import asyncio
import copy
import time
import uuid
from typing import List

import pytest

from api.dependencies import workflow_engine
from config import settings
from core import media_refs
from core.agents import storyboard_agent as sb
from core.agents.storyboard_agent import StoryboardAgent
from core.orchestrator import WorkflowStage

START_PERCENT = 5      # 阶段启动第一报（沿用现值）
FLOOR_PERCENT = 10     # 运行期下限（沿用现值）
RUN_CAP = 95           # 运行期整数封顶
EPISODE_MILESTONE_MARK = "集分镜："  # 集完成里程碑的消息特征

HB_INTERVAL = "HEARTBEAT_INTERVAL_SECONDS"
THROTTLE_INTERVAL = "PROGRESS_MIN_EVENT_INTERVAL"


# ─────────────────────────── 取证辅助 ───────────────────────────


class Recorder:
    """收集 `_report_progress` 上报：时间戳用绝对 monotonic，便于与假步骤的等待窗口对齐。"""

    def __init__(self) -> None:
        self.events: List[dict] = []

    def callback(self, phase: str, step: str, percent, data=None) -> None:
        self.events.append({
            "t": time.monotonic(),
            "phase": phase,
            "message": step,
            "percent": percent,
            "preview": bool(isinstance(data, dict) and data.get("assets_preview") is not None),
            "persist": bool(isinstance(data, dict) and data.get("persist")),
            "has_data": data is not None,
        })

    @property
    def percents(self) -> List:
        return [event["percent"] for event in self.events]

    def messages_with(self, mark: str) -> List[dict]:
        return [event for event in self.events if mark in event["message"]]

    def events_during(self, window) -> List[dict]:
        start, end = window
        return [event for event in self.events if start < event["t"] < end]


def _assert_monotonic(events: List[dict]) -> None:
    percents = [event["percent"] for event in events]
    assert all(isinstance(p, int) for p in percents), f"进度必须是整数：{percents}"
    assert all(b >= a for a, b in zip(percents, percents[1:])), f"进度不得倒退：{percents}"
    assert all(RUN_CAP >= p or p == 100 for p in percents), f"运行期不得越过 {RUN_CAP}：{percents}"


def _episode_script(ep_count: int, lines_per_episode: int = 24) -> dict:
    episodes = []
    for number in range(1, ep_count + 1):
        rows = [f"场景 {number}-1：大厅，日，内"]
        for index in range(lines_per_episode):
            rows.append(f"甲：第{number}集第{index}句台词，说明当前进展与下一步安排。")
            rows.append("<action>乙走进大厅，看向甲并在一旁坐下。</action>")
        episodes.append({
            "episode_number": number,
            "act_title": f"第{number}集",
            "content": "\n".join(rows),
        })
    return {
        "title": "探针剧本",
        "episodes": episodes,
        "characters": [{"name": "甲", "description": "甲的描述"}, {"name": "乙", "description": "乙的描述"}],
        "settings": [{"name": "大厅", "description": "大厅的描述"}],
        "meta": {},
    }


def _input_data(ep_count: int, lines_per_episode: int = 24) -> dict:
    script = _episode_script(ep_count, lines_per_episode)
    return {
        "session_id": f"probe-progress-{uuid.uuid4().hex[:12]}",
        "llm_model": "fake-model",
        "style": "anime",
        "_session_meta": {"llm_model": "fake-model", "style": "anime"},
        "_session_artifacts": {"script_generation": script, "storyboard": {"episodes": []}},
    }


def _chunks(items, count):
    count = max(1, min(count, len(items)))
    size = -(-len(items) // count)
    return [items[index:index + size] for index in range(0, len(items), size)]


class FakeSteps:
    """替换分镜的三个模型出口，保留真实结构构造与真实等待语义。"""

    def __init__(self, *, segments: int = 4, plan_sleep: float = 0.0, design_sleep: float = 0.0,
                 continuity_sleep: float = 0.0, continuity_failures: int = 0, estimate=None):
        self.segments = segments
        self.plan_sleep = plan_sleep
        self.design_sleep = design_sleep
        self.continuity_sleep = continuity_sleep
        self.continuity_failures = continuity_failures
        self.estimate = estimate
        self.plan_windows: List[tuple] = []
        self.design_windows: List[tuple] = []
        self.continuity_windows: List[tuple] = []
        self.actual_segments = 0
        self.continuity_calls = 0

    def install(self, monkeypatch) -> "FakeSteps":
        monkeypatch.setattr(StoryboardAgent, "_plan_episode_segments", self._fake_plan)
        monkeypatch.setattr(StoryboardAgent, "_design_one_segment", self._fake_design)
        # 连续性检查走真实 `_fix_episode_staging_continuity`（含重试环），只换掉模型出口
        monkeypatch.setattr(StoryboardAgent, "_query_json_object_with_retries", self._fake_object_query)
        if self.estimate is not None:
            monkeypatch.setattr(
                StoryboardAgent, "_estimate_segment_count",
                classmethod(lambda cls, ep_n, script_text, characters, settings: self.estimate),
            )
        return self

    async def _fake_plan(self, ep_n, ep_t, annotated_script, units, characters, settings,
                         llm_model, sid, planning_hint=""):
        start = time.monotonic()
        if self.plan_sleep:
            await asyncio.sleep(self.plan_sleep)
        groups = _chunks(units, self.segments)
        self.actual_segments += len(groups)
        self.plan_windows.append((start, time.monotonic()))
        return [StoryboardAgent._segment_plan_from_units(ep_n, index + 1, group)
                for index, group in enumerate(groups)]

    async def _fake_design(self, ep_n, ep_t, plan, style, llm_model, sid, planning_hint=""):
        start = time.monotonic()
        if self.design_sleep:
            await asyncio.sleep(self.design_sleep)
        self.design_windows.append((start, time.monotonic()))
        return StoryboardAgent._fallback_design_segment(ep_n, plan)

    async def _fake_object_query(self, prompt, llm_model, sid, *, label, max_retries=3):
        start = time.monotonic()
        self.continuity_calls += 1
        if self.continuity_sleep:
            await asyncio.sleep(self.continuity_sleep)
        self.continuity_windows.append((start, time.monotonic()))
        if self.continuity_calls <= self.continuity_failures:
            raise RuntimeError("探针：站位连续性检查模拟失败")
        return {"issues": [], "patches": []}


def _run_agent(monkeypatch, steps: FakeSteps, ep_count: int, *, heartbeat=0.02, throttle=0.0,
               lines_per_episode: int = 24):
    monkeypatch.setattr(sb, HB_INTERVAL, heartbeat, raising=False)
    monkeypatch.setattr(sb, THROTTLE_INTERVAL, throttle, raising=False)
    steps.install(monkeypatch)
    recorder = Recorder()
    agent = StoryboardAgent()
    agent.set_progress_callback(recorder.callback)
    result = asyncio.run(agent.process(_input_data(ep_count, lines_per_episode)))
    return recorder, result


# ─────────────────────── T1/T2：片段级逐段推进 ───────────────────────


def test_each_segment_completion_advances_percent(monkeypatch):
    """T1：单集多片段时，每个片段完成都要推进一次进度，且落在 (10, 95) 开区间内。"""
    steps = FakeSteps(segments=5)
    recorder, _ = _run_agent(monkeypatch, steps, ep_count=1)

    segment_events = recorder.messages_with("已设计完")
    assert len(segment_events) >= steps.actual_segments - 1, (
        f"每个片段完成都应上报，实际只有 {len(segment_events)} 条："
        f"{[event['message'] for event in recorder.events]}"
    )
    values = [event["percent"] for event in segment_events]
    assert values == sorted(values) and len(set(values)) >= 3, f"片段级进度应逐段递增：{values}"
    assert min(values) > FLOOR_PERCENT, f"片段完成时进度不得停留在下限 {FLOOR_PERCENT}：{values}"
    assert max(values) < RUN_CAP, f"最后一片段完成时不应已到顶：{values}"


def test_run_shows_many_distinct_percent_values(monkeypatch):
    """T2：整阶段推进的取值多样性（改动前只有 5/10/95/100 四档）。"""
    steps = FakeSteps(segments=6, plan_sleep=0.01, continuity_sleep=0.01)
    recorder, _ = _run_agent(monkeypatch, steps, ep_count=1)

    distinct = set(recorder.percents)
    assert len(distinct) >= 6, f"进度取值过少（现网症状）：{sorted(distinct)}"
    _assert_monotonic(recorder.events)
    assert recorder.events[-1]["percent"] == 100


# ─────────────────── T3：多集并发不得挤在末尾跳变 ───────────────────


def test_progress_moves_before_first_episode_milestone(monkeypatch):
    """T3：并发多集时，首集完成之前就应有非里程碑驱动的进度推进。"""
    steps = FakeSteps(segments=3, design_sleep=0.01)
    recorder, _ = _run_agent(monkeypatch, steps, ep_count=3)

    first_above = next((event for event in recorder.events if event["percent"] > FLOOR_PERCENT), None)
    assert first_above is not None, "进度全程钉在下限"
    assert EPISODE_MILESTONE_MARK not in first_above["message"], (
        f"首个越过下限的进度仍是集完成里程碑：{first_above['message']}"
    )
    _assert_monotonic(recorder.events)


# ─────────────── T4/T4b：增量式权重（估算 → 真实）不倒退 ───────────────


def test_incremental_weight_growth_keeps_progress_monotonic(monkeypatch):
    """T4：初始权重按估算登记（估算 8），规划返回真实片段数 2 后双向修正，进度不倒退。"""
    steps = FakeSteps(segments=2, estimate=8)
    recorder, _ = _run_agent(monkeypatch, steps, ep_count=1)

    _assert_monotonic(recorder.events)
    assert steps.actual_segments == 2
    planned = recorder.messages_with("已规划")
    designed = recorder.messages_with("已设计完")
    assert planned and designed, f"缺少规划/片段完成里程碑：{[e['message'] for e in recorder.events]}"
    band = [event["percent"] for event in recorder.events
            if planned[0]["t"] <= event["t"] <= designed[-1]["t"]]
    assert len(set(band)) >= 2, f"真实片段数确认后进度应随之推进，而不是钉在同一值：{band}"


def test_continuity_retry_reports_progress(monkeypatch):
    """T4b：连续性检查进入第 2 次尝试时补登记权重并发消息，期间进度不再冻死。"""
    steps = FakeSteps(segments=3, continuity_failures=1, continuity_sleep=0.01)
    recorder, _ = _run_agent(monkeypatch, steps, ep_count=1)

    assert steps.continuity_calls == 2
    retry_events = recorder.messages_with("重试")
    assert retry_events, f"重试期间没有任何进度消息：{[event['message'] for event in recorder.events]}"
    _assert_monotonic(recorder.events)


# ─────────────────────── T5：长耗时调用期间的心跳 ───────────────────────


def test_heartbeat_advances_percent_during_long_segment_wait(monkeypatch):
    """T5：单个片段设计耗时远大于心跳间隔时，等待期内进度要持续推进。"""
    steps = FakeSteps(segments=1, design_sleep=0.5)
    recorder, _ = _run_agent(monkeypatch, steps, ep_count=1, heartbeat=0.02, throttle=0.0)

    window = steps.design_windows[-1]
    during = recorder.events_during(window)
    assert len(during) >= 3, f"片段设计等待期内心跳事件不足：{during}"
    values = [event["percent"] for event in during]
    assert len(set(values)) >= 3, f"等待期内进度取值未推进（现网症状）：{values}"
    _assert_monotonic(recorder.events)


def test_heartbeat_advances_percent_during_continuity_wait(monkeypatch):
    """T5b：连续性检查是最长的一步（现网 10 分钟），等待期内必须有心跳推进。"""
    steps = FakeSteps(segments=2, continuity_sleep=0.5)
    recorder, _ = _run_agent(monkeypatch, steps, ep_count=1, heartbeat=0.02, throttle=0.0)

    window = steps.continuity_windows[-1]
    during = recorder.events_during(window)
    assert len(during) >= 3, f"连续性检查等待期内心跳事件不足：{during}"
    assert len(set(event["percent"] for event in during)) >= 3


def test_heartbeat_capped_by_pending_work(monkeypatch):
    """T5c：心跳只能在"当前档位 → 下一步完成应到的档位"内渐近推进，不得越过未完成的工作。"""
    steps = FakeSteps(segments=4, design_sleep=0.3)
    recorder, _ = _run_agent(monkeypatch, steps, ep_count=1, heartbeat=0.02, throttle=0.0)

    designed = recorder.messages_with("已设计完")
    assert len(designed) == steps.actual_segments, f"每个片段完成都应上报：{len(designed)}"
    first_done, last_done = designed[0], designed[-1]
    before = [event for event in recorder.events if event["t"] < first_done["t"]]
    assert all(event["percent"] <= first_done["percent"] for event in before), (
        f"首个片段返回前不得已到它完成后的档位：{[event['percent'] for event in before]} > {first_done['percent']}"
    )
    # 推进必须真实发生（不靠后续里程碑补发），但又没偷跑完剩余工作
    assert before[-1]["percent"] > before[0]["percent"], f"等待期内进度未推进：{[e['percent'] for e in before]}"
    assert first_done["percent"] < last_done["percent"], "全部片段完成时应比首个片段完成时更高"


# ─────────────── T6/T7：进度源单体行为（节流 / 封顶 / 单调） ───────────────


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


def _ledger(report, *, clock=None, min_interval=0.0):
    return sb._StoryboardProgress(report, min_interval=min_interval, clock=clock or time.monotonic)


def test_ledger_is_single_source_and_monotonic():
    """T6a：所有上报都出自同一进度源；分母增长（增量登记）不得让进度回落。"""
    seen: List[tuple] = []
    clock = _Clock()
    ledger = _ledger(lambda message, percent, data=None: seen.append((message, percent)), clock=clock)

    ledger.begin("开始设计 1 集的分镜...")
    assert seen[-1][1] == START_PERCENT
    ledger.add_total(10)
    ledger.note("准备生成分镜...")
    assert seen[-1][1] == FLOOR_PERCENT
    ledger.complete(1)
    after_one = seen[-1][1]
    ledger.add_total(20)          # 认知增长：分母变大，比值变小
    ledger.note("规划完成")
    assert seen[-1][1] >= after_one, f"增量登记后进度回落：{seen}"
    ledger.complete(5)
    assert seen[-1][1] > after_one


def test_ledger_throttles_heartbeat_events():
    """T6b：心跳只受节流窗口约束——窗口内不发，窗口后即使 percent 不变也要发。

    已知边界（不是验收目标）：当单步工作量不足 1 个百分点时，长调用期间进度数值就是
    静止的，只有消息在动（无 token 级回调下的物理上限）；本例用 total=100 固定住这个行为，
    以免日后把它当成“数值也在推进”误报。
    """
    seen: List[tuple] = []
    clock = _Clock()
    ledger = _ledger(lambda message, percent, data=None: seen.append((message, percent)),
                     clock=clock, min_interval=4.0)
    # 单步只值 int(85/100)=0 个百分点：档位推不动，只能靠消息续命
    ledger.add_total(100)

    assert ledger.beat("正在设计分镜") is True, "首个心跳应发出"
    assert ledger.beat("正在设计分镜") is False, "时钟未前进时不得重复发送（节流）"
    clock.advance(4.0)
    assert ledger.beat("正在设计分镜") is True, "节流窗口过后即使 percent 不变也要发"
    assert ledger.percent == FLOOR_PERCENT, f"量程内无预算时不得预支档位：{ledger.percent}"

    emitted = len(seen)
    for _ in range(30):
        clock.advance(4.0)
        assert ledger.beat("正在设计分镜") is True, "长调用期间不得静默"
    assert len(seen) == emitted + 30
    # 超过 WAIT_HINT_AFTER 后消息自带等待时长，文本仍在变（前两条为尚未到阈值的原样消息）
    assert any("已等待" in message for message, _ in seen[2:])
    assert seen[-1][0] != seen[-2][0], "心跳消息必须随等待时长变化"
    assert all(isinstance(percent, int) for _, percent in seen)


def _drive_beats(total: int, done: int, *, ticks: int = 75, interval: float = 8.0,
                min_interval: float = 4.0):
    """用生产类 + 虚拟时钟按生产比例驱动心跳：8s tick × 75 次 = 600 秒长调用。"""
    clock = _Clock()
    events: List[tuple] = []
    ledger = _ledger(lambda message, percent, data=None: events.append((clock.now, percent, message)),
                     clock=clock, min_interval=min_interval)
    ledger.add_total(total)
    ledger.done = done
    ledger.note("上一个里程碑")           # 把档位推到已完成工作量应有的值
    for _ in range(ticks):
        clock.advance(interval)
        ledger.beat("正在检查人物站位连续性")
    return ledger, events


def test_heartbeat_never_goes_silent_during_long_call():
    """T5d：真实比例（8s tick，600 秒）的长调用不得静默，审核复现过 560/592 秒静默。

    注意：这里量的是「事件间隔」（消息是否在动），不是「数值是否在动」；后者的静止是
    已知边界（同一组实测里单集数值停滞 584 秒、三集 600 秒），已在设计文档里标明。
    """
    for total, done, name in ((18, 17, "单集最后一步"), (54, 51, "三集并发最后几步")):
        ledger, events = _drive_beats(total, done)
        percents = [percent for _, percent, _ in events]
        assert len(events) >= 70, f"{name}：600 秒长调用中心跳事件过少（静默）：{len(events)}"
        gaps = [b[0] - a[0] for a, b in zip(events, events[1:]) if a[1] == b[1]]
        assert max(gaps) <= 8.0 * 1.5, f"{name}：最大静默 {max(gaps):.0f}s 超阀"
        assert percents == sorted(percents), f"{name}：进度倒退：{percents}"
        assert all(isinstance(percent, int) for percent in percents)


def test_heartbeat_does_not_prespend_finished_step():
    """T5e：最后一步未完成时，心跳不得把进度推到该步完成后的 95%（审核复现过 40s 到 95）。"""
    ledger, events = _drive_beats(18, 17)
    percents = [percent for _, percent, _ in events]
    band, ceiling = ledger._band(), ledger._beat_ceiling()
    assert band == 95 - 5 and ceiling == 92, f"量程应只预支一半：band={band} ceiling={ceiling}"
    assert max(percents) <= ceiling, f"心跳越过了预支上限：{max(percents)} > {ceiling}"
    assert RUN_CAP not in percents, f"未完成任务却摸到 {RUN_CAP}%：{percents}"


def test_overestimate_is_clawed_back_without_finish_jump():
    """T5f：估算偏大（K̂=28、真实 2）时分母要收回，收尾档到 95、finish 跳变 ≤ 6 点。

    调用顺序与生产一致：规划返回后先 add_total(差值) 修正分母，再报「已规划」里程碑。
    """
    seen: List[tuple] = []
    ledger = _ledger(lambda message, percent, data=None: seen.append((message, percent)))

    ledger.add_total(2 + 28)          # 初始权重：规划 1 + 估算 28 + 连续性 1
    ledger.add_total(2 - 28)          # 真实片段数已知：差值为负
    assert ledger.total == 1 + 2 + 1, f"分母未收回：{ledger.total}"
    ledger.complete(1, "已规划片段", key="ep1:plan")
    planned = seen[-1][1]
    ledger.complete(1, "片段 1")
    ledger.complete(1, "片段 2")
    ledger.complete(1, "连续性检查")
    last_running = seen[-1][1]
    ledger.finish()

    assert planned > FLOOR_PERCENT, f"修正后的规划里程碑应反映真实工作量：{planned}"
    assert last_running == RUN_CAP, f"收尾档位被虚高分母拉低：{last_running}"
    assert 100 - last_running <= 6, f"收尾跳变过大：{last_running} → 100"


def test_shrink_is_applied_immediately_without_lies():
    """T5h：收缩虚高分母当场生效、不做假平滑；只守不变量（不倒退 / 不虚报做完 / 收尾只差一档）。

    审核建议的“单次跳幅封顶”经实测不自洽：小 total 下「正常完成一步」天然就能跳
    11–21 个百分点（total=4 时 52→73），只封顶收缩反而会把同一个真实修正推迟到更晚、
    攒成收尾大跳。因此本例不断言跳幅大小，只断言“报出来的就是真实档位”。
    """
    seen: List[tuple] = []
    ledger = _ledger(lambda message, percent, data=None: seen.append((message, percent)))

    # 两集：ep1 估算严重偏大（K̂=28→真 2），ep2 估算偏小（K̂=1→真 3）
    ledger.add_total((2 + 28) + (2 + 1))          # 初始 33
    ledger.complete(1, "ep2 已规划 3 个片段", key="ep2:plan")
    ledger.add_total(3 - 1)                        # ep2 放大 → 35
    ledger.complete(1, "ep1 已规划 2 个片段", key="ep1:plan")
    before = ledger.percent
    ledger.add_total(2 - 28)                       # ep1 收缩 → 9（本身不发事件，percent 不动）
    assert ledger.percent == before, "分母修正本身不是工作事件，不得自己推高进度"
    ledger.note("开始设计 ep1 片段")                 # 下一个真实事件才把修正后的档位报出去

    percents = [percent for _, percent in seen]
    assert percents == sorted(percents), f"收缩后进度倒退：{percents}"
    assert (ledger.done, ledger.total) == (2, 9), (
        f"收缩必须只改分母且账目自洽：done={ledger.done} total={ledger.total}（期望 2/9）"
    )
    assert ledger.percent == ledger._band() == 10 + int(85 * 2 / 9), (
        f"报出来的必须就是真实档位：{ledger.percent}"
    )
    assert ledger.percent > before, "收缩后的档位应高于收缩前（同一真实工作量下分母变小）"

    reached_cap = None
    while ledger.done < ledger.total:
        ledger.complete(1, "完成一步")
        if reached_cap is None and seen[-1][1] == RUN_CAP:
            reached_cap = ledger.done
        assert ledger.percent == ledger._band()

    assert ledger.done == ledger.total == 9, f"记账未用尽：{ledger.done}/{ledger.total}"
    assert reached_cap == 9, f"{RUN_CAP}% 必须落在最后一个真实步，实际 done={reached_cap}"
    ledger.finish()
    assert seen[-1][1] == 100 and 100 - seen[-2][1] <= 6, (
        f"收尾大跳：{seen[-2][1]} → 100"
    )


def test_growth_keeps_progress_moving_not_frozen():
    """T5g：分母增长时不得虚报完成：用比例下限防回落，但 MUST NOT 改 done。

    上一版用重定基把 done 从 1 抬到 5，造成「真实 14/18 步就报 95%」；现在接受“档位
    先停一会儿”作为代价，换取 done/total 始终是真实步数、且 95% 等价于真做完。
    """
    seen: List[tuple] = []
    ledger = _ledger(lambda message, percent, data=None: seen.append((message, percent)))

    ledger.add_total(2 + 2)           # 估算只有 2 个片段
    ledger.complete(1, "已规划片段", key="ep1:plan")
    planned = seen[-1][1]
    ledger.add_total(16 - 2)          # 真实 16 个片段：分母放大

    assert ledger.done == 1, f"分母修正不得改动已完成步数：done={ledger.done}"
    assert ledger.total == 1 + 16 + 1, f"分母未反映真实工作：{ledger.total}"
    assert seen[-1][1] == planned, "增长分母本身不是工作事件，不应推高进度"

    real_steps = 1
    done_at_cap = None
    while ledger.done < ledger.total:
        real_steps += 1
        ledger.complete(1, f"第 {real_steps} 步")
        if done_at_cap is None and seen[-1][1] == RUN_CAP:
            done_at_cap = ledger.done

    assert ledger.done == ledger.total, f"记账不完就不该跑完循环：{ledger.done}/{ledger.total}"
    assert done_at_cap == ledger.total, (
        f"percent 首次到 {RUN_CAP} 时 done 应为 {ledger.total}，实际 {done_at_cap}"
    )


def _wait_seconds(text: str) -> int:
    """从心跳消息里取已等待秒数（支持「N 秒」与「M 分 SS 秒」两种写法）。"""
    assert "已等待" in text, f"消息里没有等待时长：{text}"
    tail = text.split("已等待")[-1].strip()
    if "分" in tail:
        minutes, _, rest = tail.partition("分")
        return int(minutes.strip() or 0) * 60 + int(rest.replace("秒", "").strip() or 0)
    return int(tail.replace("秒", "").strip() or 0)


def test_wait_timer_is_scoped_per_step():
    """T5i：等待计时按步隔离——别的集（甚至同集另一步）的事件不得把本集计时归零。"""
    seen: List[tuple] = []
    clock = _Clock()
    ledger = _ledger(lambda message, percent, data=None: seen.append((message, percent)), clock=clock)
    ledger.add_total(60)

    key_a = "ep1:continuity"
    beat_a = "第 1 集正在检查人物站位连续性，模型仍在返回"
    for _ in range(5):
        clock.advance(8.0)
        ledger.beat(beat_a, key=key_a)
    first = seen[-1][0]
    assert "已等待" in first, f"第 1 集心跳本应已起等待计时：{first}"

    # 并发跳入的其它集事件：全局里程碑（无 key）与另一集的带 key 事件
    clock.advance(8.0)
    ledger.note("第 2 集分镜已设计完 3/6 个片段")
    clock.advance(8.0)
    ledger.complete(1, "第 3 集已规划 4 个片段", key="ep3:plan")

    clock.advance(8.0)
    ledger.beat(beat_a, key=key_a)
    # 起点是第 1 集的首个心跳（t=8），至 t=64 已等 56 秒；别的集的事件不得把它归零
    assert _wait_seconds(seen[-1][0]) == 56, f"第 1 集的等待时长应为连续累计：{seen[-1][0]}"
    assert _wait_seconds(seen[-1][0]) > _wait_seconds(first), (
        f"别的集的事件重置了第 1 集的计时：{first} → {seen[-1][0]}"
    )


def test_multi_episode_with_bidirectional_corrections():
    """T5j：多集并发 × 双向修正的组合：不倒退、不越界跳幅、95% 仍等价于真做完。"""
    seen: List[tuple] = []
    ledger = _ledger(lambda message, percent, data=None: seen.append((message, percent)))

    # 两集：ep1 估算严重偏大（K̂=28→真 2），ep2 估算偏小（K̂=1→真 12）
    ledger.add_total((2 + 28) + (2 + 1))
    ledger.complete(1, "ep1 已规划", key="ep1:plan")
    ledger.add_total(2 - 28)          # ep1 收缩
    ledger.complete(1, "ep2 已规划", key="ep2:plan")
    ledger.add_total(12 - 1)          # ep2 放大

    percents = [percent for _, percent in seen]
    assert percents == sorted(percents), f"双向修正后进度倒退：{percents}"
    assert ledger.percent == ledger._band(), "档位必须等于真实比例，不做假平滑"

    real_steps = seen[-1][1]
    done_at_cap = None
    while ledger.done < ledger.total:
        ledger.complete(1, "完成一步")
        if done_at_cap is None and seen[-1][1] == RUN_CAP:
            done_at_cap = ledger.done
        assert ledger.percent == ledger._band()

    assert ledger.done == ledger.total, f"记账不匹配：{ledger.done}/{ledger.total}"
    assert ledger.total == 1 + 2 + 1 + 1 + 12 + 1, f"分母未同时体现收缩与放大：{ledger.total}"
    # 核心不变量：95% 只允许在真实记账步全部完成的那一刻出现
    assert done_at_cap == ledger.total, (
        f"首次到 {RUN_CAP}% 时 done 应为 {ledger.total}，实际 {done_at_cap}"
    )
    assert real_steps <= RUN_CAP


def test_ledger_running_cap_and_finish():
    """T7：运行期整数封顶 95；只有 finish 报 100；total 为 0 时不炸。"""
    seen: List[tuple] = []
    ledger = _ledger(lambda message, percent, data=None: seen.append((message, percent)))

    ledger.add_total(1)
    for _ in range(20):
        ledger.complete(1)
        ledger.beat("心跳")
    running = [percent for _, percent in seen]
    assert all(isinstance(percent, int) for percent in running)
    assert max(running) <= RUN_CAP, f"运行期越过 {RUN_CAP}：{running}"
    ledger.finish()
    assert seen[-1][1] == 100

    empty = _ledger(lambda *a, **k: None)
    empty.note("无工作量")      # MUST NOT 抛错
    assert empty.percent == FLOOR_PERCENT


def test_segment_level_events_carry_no_preview_or_persist(monkeypatch):
    """T8：新增的片段级/心跳事件不得携带 assets_preview/persist（落库节奏不变）。"""
    steps = FakeSteps(segments=5, continuity_sleep=0.01)
    recorder, _ = _run_agent(monkeypatch, steps, ep_count=2)

    # 只有两类里程碑可以携带落库数据：开头的清单预览 + 每集完成时的预览
    milestone_marks = (EPISODE_MILESTONE_MARK, "准备生成分镜")
    extra = [event for event in recorder.events
             if not any(mark in event["message"] for mark in milestone_marks) and event["has_data"]]
    assert not extra, f"非里程碑事件不应携带落库数据：{extra}"
    persisted = [event for event in recorder.events if event["persist"]]
    assert len(persisted) == 1 + 2, f"带 persist 的预览事件数应仍为 1（准备）+ 集数：{persisted}"
    assert all(event["phase"] == "分镜设计" for event in recorder.events), \
        f"阶段名应统一：{sorted({event['phase'] for event in recorder.events})}"


def test_retry_paths_keep_progress_monotonic(monkeypatch):
    """T9：含连续性重试的全路径下，进度仍是整数、单调、运行期封顶。"""
    steps = FakeSteps(segments=4, estimate=2, continuity_failures=1, design_sleep=0.01,
                      continuity_sleep=0.05)
    recorder, result = _run_agent(monkeypatch, steps, ep_count=2, heartbeat=0.02, throttle=0.0)

    _assert_monotonic(recorder.events)
    episodes = result["payload"]["episodes"]
    assert [ep["episode_number"] for ep in episodes] == [1, 2]
    for episode in episodes:
        numbers = [segment["segment_number"] for segment in episode["segments"]]
        assert numbers == sorted(numbers), f"片段顺序仍须按 segment_number：{numbers}"


# ──────────── T10：心跳从 script_agent 抽出后逐值一致 ────────────


def test_shared_heartbeat_matches_script_agent_ascent():
    """T10：`AgentInterface._await_with_heartbeat` 的推进值与原 script_agent 实现逐值一致。"""
    logged: List[tuple] = []

    async def slow():
        for _ in range(5):
            await asyncio.sleep(0.05)
        return "done"

    async def scenario():
        agent = StoryboardAgent()
        return await agent._await_with_heartbeat(
            slow(), start=20.0, cap=78.0, message="模型正在生成修订稿，请稍候...",
            log=lambda percent, message: logged.append((percent, message)),
            interval=0.02,
        )

    assert asyncio.run(scenario()) == "done"
    assert logged, "长耗时调用期间没有任何心跳"

    expected = []
    percent = 20.0
    for _ in range(len(logged)):
        percent = min(78.0, percent + max(1.0, (78.0 - percent) * 0.12))
        expected.append(percent)
    assert [value for value, _ in logged] == pytest.approx(expected[:len(logged)]), \
        f"心跳推进值与原实现不一致：{[v for v, _ in logged]} vs {expected[:len(logged)]}"
    assert all(message == "模型正在生成修订稿，请稍候..." for _, message in logged)
    assert logged[-1][0] <= 78.0


def test_heartbeat_abandons_task_on_outer_cancellation():
    """T10b：外层被取消时不留无人认领的任务（沿用原实现的 finally 语义）。"""
    started = asyncio.Event()
    cancelled = {"hit": False}

    async def never_ends():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled["hit"] = True
            raise
        return "never"

    async def scenario():
        agent = StoryboardAgent()
        outer = asyncio.ensure_future(
            agent._await_with_heartbeat(never_ends(), start=0.0, cap=10.0, message="心跳",
                                        log=lambda p, m: None, interval=0.01)
        )
        await started.wait()
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer

    asyncio.run(scenario())
    assert cancelled["hit"] is True, "内部任务未被取消"


# ─────────────── T11b：片段数估算器的口径 ───────────────


def test_segment_estimator_matches_deterministic_packing():
    """T11b：估算与兜底打包同源（不调模型）、长剧本贴近真实分段数、异常正文不抛错。"""
    content = _episode_script(1, 24)["episodes"][0]["content"]
    script = _episode_script(1, 24)
    characters, settings_list = script["characters"], script["settings"]

    _, units = StoryboardAgent._annotate_episode_script(content, characters, settings_list)
    baseline = len(StoryboardAgent._fallback_segment_plan(1, units))
    assert StoryboardAgent._estimate_segment_count(1, content, characters, settings_list) == baseline

    # 真实剧本规模下不得像正则拆分那样报成 1 段（那是分母估小、finish 大跳的根源）
    assert StoryboardAgent._estimate_segment_count(1, content, characters, settings_list) > 1

    assert StoryboardAgent._estimate_segment_count(1, "", characters, settings_list) == 1
    assert StoryboardAgent._estimate_segment_count(1, "没有场景也没有台词的一整块文本", [], []) >= 1


# ─────────────── T12：编排器漏斗层（API 级）取证 ───────────────

OWNER = "progress_probe_owner"


@pytest.fixture(autouse=True)
def isolated_roots(tmp_path, monkeypatch):
    """执行根/结果根隔离：不触碰宿主 data/ 与业务桶。"""
    code_root = tmp_path / "code"
    for path in (code_root / "result", code_root / "temp", code_root / "work" / "executions"):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "CODE_DIR", str(code_root))
    monkeypatch.setattr(settings, "RESULT_DIR", str(code_root / "result"))
    monkeypatch.setattr(settings, "TEMP_DIR", str(code_root / "temp"))
    refs_root = code_root / "work" / "_refs"
    refs_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(media_refs, "_REFS_ROOT", refs_root)
    yield tmp_path


def test_engine_level_progress_and_no_extra_persist(monkeypatch):
    """T12：经 `execute_stage` 真实漏斗执行一次分镜，进度细粒度可见且落库次数不增加。"""
    monkeypatch.setattr(sb, HB_INTERVAL, 0.02, raising=False)
    monkeypatch.setattr(sb, THROTTLE_INTERVAL, 0.0, raising=False)
    steps = FakeSteps(segments=4, design_sleep=0.01, continuity_sleep=0.01)
    steps.install(monkeypatch)

    session_id = f"sp{uuid.uuid4().hex[:10]}"
    script = _episode_script(1)
    workflow_engine.create_session(session_id, {"idea": "探针", "llm_model": "fake-model",
                                                 "style": "anime"}, owner=OWNER)

    def seed(state) -> None:
        state.artifacts["script_generation"] = copy.deepcopy(script)
        state.status["script_generation"] = "completed"

    workflow_engine._mutate(session_id, seed)
    state = workflow_engine.get_state(session_id)

    persist_calls = {"n": 0}
    original_persist = workflow_engine._persist_partial

    def counting_persist(run, preview):
        persist_calls["n"] += 1
        return original_persist(run, preview)

    monkeypatch.setattr(workflow_engine, "_persist_partial", counting_persist)

    recorder = Recorder()
    input_data = {"session_id": session_id, "llm_model": "fake-model", "style": "anime"}
    asyncio.run(workflow_engine.execute_stage(state, WorkflowStage.STORYBOARD, input_data,
                                              progress_callback=recorder.callback))

    values = recorder.percents
    assert len(set(values)) >= 5, f"漏斗层可见的进度取值过少：{sorted(set(values))}"
    _assert_monotonic(recorder.events)
    assert persist_calls["n"] <= 2, f"逐片段事件不应增加落库次数：{persist_calls['n']}"
    after = workflow_engine.get_state(session_id)
    assert after.stage_progress["storyboard"]["percent"] == 100
