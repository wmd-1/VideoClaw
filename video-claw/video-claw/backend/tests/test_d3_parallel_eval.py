"""D3：确定性分集优先级与评估并行开关。"""
from __future__ import annotations

import asyncio
import threading

from core.agents.script_agent import ScriptWriterAgent
from core.script_import import decide_episode_starts, detect_candidates, split_episodes


def test_explicit_episode_titles_win_over_partial_llm():
    lines = ["第1集 开端", "正文一", "第2集 继续", "正文二", "第3集 收尾", "正文三"]
    candidates = detect_candidates(lines)
    starts, decided_by = decide_episode_starts(candidates, llm_lines=[candidates[0]["line"]])
    assert decided_by == "rule"
    episodes = split_episodes(lines, starts)
    assert [item["episode_number"] for item in episodes] == [1, 2, 3]
    assert "\n".join(item["content"] for item in episodes) == "\n".join(lines)


def test_evaluate_pair_parallel_and_sequential_order():
    agent = ScriptWriterAgent()
    order = []

    def fake_query(llm, prompt, images, model, safe, sid, web_search):
        order.append(prompt)
        return f"result-{prompt}"

    agent._cancellable_query = fake_query
    dialogue, plot = asyncio.run(agent._evaluate_pair(
        object(), "D", "P", "model", "sid", False, True,
    ))
    assert (dialogue, plot) == ("result-D", "result-P")

    order.clear()
    dialogue, plot = asyncio.run(agent._evaluate_pair(
        object(), "D", "P", "model", "sid", False, False,
    ))
    assert order == ["D", "P"]

    started = []
    barrier = threading.Barrier(2)

    def blocking_query(llm, prompt, images, model, safe, sid, web_search):
        started.append(prompt)
        barrier.wait(timeout=2)
        return prompt.lower()

    agent._cancellable_query = blocking_query
    dialogue, plot = asyncio.run(agent._evaluate_pair(
        object(), "D", "P", "model", "sid", False, True,
    ))
    assert set(started) == {"D", "P"}
    assert (dialogue, plot) == ("d", "p")


def test_concurrency_flag_and_parallel_failure():
    assert ScriptWriterAgent._evaluation_concurrency({}) is True
    assert ScriptWriterAgent._evaluation_concurrency({"enable_concurrency": False}) is False

    agent = ScriptWriterAgent()
    calls = []

    def failing_query(llm, prompt, images, model, safe, sid, web_search):
        calls.append(prompt)
        if prompt == "D":
            raise RuntimeError("dialogue failed")
        return "plot-ok"

    agent._cancellable_query = failing_query
    try:
        asyncio.run(agent._evaluate_pair(
            object(), "D", "P", "model", "sid", False, True,
        ))
    except RuntimeError as exc:
        assert "dialogue failed" in str(exc)
    else:
        raise AssertionError("parallel evaluation must surface the exception")
    assert set(calls) == {"D", "P"}, "并行失败时另一条已提交的调用会完成"
