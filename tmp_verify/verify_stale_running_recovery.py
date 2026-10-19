# workflow-stale-running-recovery 验收（既有 Docker 后端容器内、零花费、隔离临时目录）
# 覆盖 tasks 2.1/2.2/3.1/3.2/3.3/4.1 的核心逻辑；不触碰真实 8 会话（屏蔽 __init__ 批量加载 + 临时 session 目录）。
# 运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_stale_running_recovery.py
import json
import os
import sys
import tempfile

sys.path.insert(0, "/app")
os.chdir("/app")

from core.orchestrator import WorkflowEngine, WorkflowState, WorkflowStage

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        failures.append(name)


# 构造引擎但屏蔽真实批量加载，避免 __init__ 归一/落盘真实会话；改用临时目录
_orig_load = WorkflowEngine._load_sessions_from_disk
WorkflowEngine._load_sessions_from_disk = lambda self: None
try:
    engine = WorkflowEngine()
finally:
    WorkflowEngine._load_sessions_from_disk = _orig_load
TMP = tempfile.mkdtemp(prefix="vc-stale-")
engine._session_dir = TMP
CURRENT = engine._boot_id


def write_session(sid, status, boot_id, artifacts=None, stage_progress=None, legacy_str=False):
    data = {"session_id": sid, "current_stage": "script_generation",
            "artifacts": artifacts or {}, "stage_progress": stage_progress or {},
            "updated_at": 1.0}
    if legacy_str:
        data["status"] = status  # 传入字符串（历史遗留格式）
    else:
        data["status"] = status
    if boot_id != "__ABSENT__":
        data["boot_id"] = boot_id
    with open(os.path.join(TMP, f"{sid}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def read_session(sid):
    with open(os.path.join(TMP, f"{sid}.json"), encoding="utf-8") as f:
        return json.load(f)


def fresh_get_state(sid):
    engine.sessions.pop(sid, None)
    return engine.get_state(sid)


print("=" * 78)
print("【2.1】boot_id 随 running 落盘、终态后清除（持久化接线）")
s = WorkflowState("t-persist")
s.status["script_generation"] = "running"
s.boot_id = CURRENT
engine.sessions["t-persist"] = s
engine.save_session_to_disk("t-persist")
check("2.1a 执行中落盘含 boot_id == 当前进程", read_session("t-persist").get("boot_id") == CURRENT,
      str(read_session("t-persist").get("boot_id")))
s.status["script_generation"] = "waiting"
s.boot_id = None
engine.save_session_to_disk("t-persist")
check("2.1b 终态后落盘 boot_id 为 None", read_session("t-persist").get("boot_id") is None,
      str(read_session("t-persist").get("boot_id")))

print("\n【3.1】_recover_stale_running 四种阶段态逐一断言只有一种被改")
# running 无主
s1 = WorkflowState("t-r-orphan")
s1.status["script_generation"] = "running"
s1.boot_id = "OTHER-BOOT"
c1 = engine._recover_stale_running(s1)
check("3.1a running 无主 → stopped 且 changed=True",
      c1 and s1.status["script_generation"] == "stopped", s1.status["script_generation"])
# running 有主（同进程）
s2 = WorkflowState("t-r-owned")
s2.status["script_generation"] = "running"
s2.boot_id = CURRENT
c2 = engine._recover_stale_running(s2)
check("3.1b running 有主（boot_id==当前）→ 不改", (not c2) and s2.status["script_generation"] == "running")
# waiting / completed 不动
s3 = WorkflowState("t-wait")
s3.status["script_generation"] = "waiting"
s3.boot_id = "OTHER-BOOT"
c3 = engine._recover_stale_running(s3)
s4 = WorkflowState("t-done")
s4.status["script_generation"] = "completed"
s4.boot_id = "OTHER-BOOT"
c4 = engine._recover_stale_running(s4)
check("3.1c waiting/completed 即使 boot_id 不符也不改", (not c3) and (not c4)
      and s3.status["script_generation"] == "waiting" and s4.status["script_generation"] == "completed")

print("\n【2.2】旧格式无 boot_id：dict 与 str 两种 status 均被归一、不抛错")
write_session("t-legacy-dict", {"script_generation": "running", "storyboard": "pending"}, "__ABSENT__")
st = fresh_get_state("t-legacy-dict")
check("2.2a dict 格式缺 boot_id 的 running → stopped（不抛错）",
      st is not None and st.status["script_generation"] == "stopped",
      str(st.status) if st else "None")
write_session("t-legacy-str", "running", "__ABSENT__", legacy_str=True)
st2 = fresh_get_state("t-legacy-str")
check("2.2b str 遗留格式（current_stage 置 running）缺 boot_id → stopped（不抛错）",
      st2 is not None and st2.status["script_generation"] == "stopped",
      str(st2.status) if st2 else "None")

print("\n【3.2/3.3】两入口一致 + 幂等（重复恢复只改一次、不重复写盘）")
write_session("t-idem", {"script_generation": "running"}, "OLD-BOOT")
first = fresh_get_state("t-idem")
after_first = read_session("t-idem")
size_first = len(json.dumps(after_first))
check("3.2a 懒加载首次即返回归一后 stopped（无 running 闪现）",
      first.status["script_generation"] == "stopped")
check("3.2b 归一结果已落盘（boot_id 清除、状态 stopped）",
      after_first["status"]["script_generation"] == "stopped" and after_first.get("boot_id") is None)
# 第二次访问（已在内存）+ 强制再恢复一次
again = engine.get_state("t-idem")
c_again = engine._recover_stale_running(again)
check("3.3a 重复恢复 changed=False（幂等）", c_again is False and again.status["script_generation"] == "stopped")
size_second = len(json.dumps(read_session("t-idem")))
check("3.3b 重复恢复不重复写盘（文件字节数不变）", size_first == size_second, f"{size_first} vs {size_second}")

print("\n【4.1】在途执行（boot_id==当前）与后台重生成（阶段非 running）不被误伤")
# 在途：内存中 running 有主，恢复逻辑触及也不改
s_live = WorkflowState("t-live")
s_live.status["video_generation"] = "running"
s_live.boot_id = CURRENT
engine.sessions["t-live"] = s_live
check("4.1a 在途执行（有主）恢复逻辑不改", engine._recover_stale_running(s_live) is False
      and s_live.status["video_generation"] == "running")
# 后台重生成：阶段 waiting、items 内含 running 条目，但阶段 status 无 running → 不在判定范围
s_bg = WorkflowState("t-bg")
s_bg.status["prompt_rewrite"] = "waiting"
s_bg.boot_id = None
s_bg.artifacts = {"prompt_rewrite": {"items": [{"id": "seg_1", "status": "running"}]}}
check("4.1b 后台重生成（阶段非 running）不被归一", engine._recover_stale_running(s_bg) is False
      and s_bg.status["prompt_rewrite"] == "waiting")

print("\n【启动批量入口一致】")
write_session("t-batch", {"storyboard": "running"}, "OLD-BOOT")
engine2 = WorkflowEngine.__new__(WorkflowEngine)  # 直接跑批量加载验证第二入口
engine2.sessions = {}
engine2._stop_events = {}
engine2._active_sessions = set()
engine2._background_tasks = set()
import threading
engine2._state_lock = threading.RLock()
engine2._session_dir = TMP
engine2._boot_id = CURRENT
engine2._load_sessions_from_disk()
check("3.2c 启动批量入口同样归一（storyboard running→stopped）",
      engine2.sessions.get("t-batch") is not None
      and engine2.sessions["t-batch"].status["storyboard"] == "stopped")

print("=" * 78)
if failures:
    print(f"FAILED {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
