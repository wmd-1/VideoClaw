# 临时 UI 取证脚本（非产品代码）：给「提示词改写」阶段注入 9 条条目 + running 进度
# 目的：在没有真实分镜/参考图产物的环境里，验证前端两处修复
#   1) 条目列表可上下滚动（此前根容器缺 overflow 容器，超一屏被裁切）
#   2) 阶段执行中显示进度条与「已改写 n/N 条」
# 用法（项目测试规则：在既有后端容器内执行）：
#   docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/seed_rewrite_ui.py
# 之后 docker compose restart backend（get_status_snapshot 只读内存，需重载磁盘会话）
# 清理：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/unseed_rewrite_ui.py
import json
import os
import time

SESSION_ID = "zz-ui-rewrite-9items"
SESSION_DIR = "/app/code/data/sessions"
PATH = os.path.join(SESSION_DIR, SESSION_ID + ".json")

LONG_BODY = (
    "0.00s-8.00s. A rain-soaked narrow lane at night, eye-level slow tracking shot following "
    "the subject from behind, sodium-vapour lamps reflecting on wet asphalt, shallow depth of "
    "field at 50mm, distant trambell sound, warm rim light on the shoulder contour, cobblestone "
    "texture and damp brick wall visible on the left, the subject's coat hem swings twice. "
)


def make_item(i: int) -> dict:
    status = "failed" if i == 8 else ("pending" if i == 9 else "done")
    body = (LONG_BODY * 3)[:1400] if status == "done" else ""
    item = {
        "id": f"seg_01_{i:02d}",
        "name": f"第1集-片段{i}",
        "index": i,
        "episode": 1,
        "original_prompt": f"分镜 {i}：雨夜小巷，主角背身缓行，远处电车声。",
        "rewritten_prompt": body,
        "input_mode": "I2VA",
        "duration": 8,
        "status": status,
        "selected": "",
        "versions": [{"content": body[:120], "source": "agent", "created_at": "2026-10-09T10:00:00"}] if body else [],
        "grounding": {"described": False, "text_only": True},
    }
    if status == "failed":
        item["error"] = "结构校验未通过：正文含抽象词 cinematic"
    return item


segments = [
    {
        "segment_id": f"seg_01_{i:02d}",
        "shots": [{"content": f"镜头{i}：雨夜小巷缓行", "duration": 8}],
        "total_duration": 8,
        "characters": ["主角"],
        "setting": "雨夜后街",
    }
    for i in range(1, 10)
]

state = {
    "session_id": SESSION_ID,
    "current_stage": "prompt_rewrite",
    "status": {
        "script_generation": "completed",
        "character_design": "completed",
        "storyboard": "completed",
        "reference_generation": "completed",
        "prompt_rewrite": "running",
        "video_generation": "pending",
        "post_production": "pending",
    },
    "error": None,
    "artifacts": {
        "storyboard": {"episodes": [{"episode_number": 1, "segments": segments}]},
        "prompt_rewrite": {"items": [make_item(i) for i in range(1, 10)]},
    },
    "meta": {
        "idea": "UI 取证临时会话（9 条改写条目）",
        "style": "realistic",
        "video_generation_mode": "first_frame",
        "llm_model": "deepseek-v4.1-flash",
        "vlm_model": "local-vlm",
    },
    "stage_progress": {
        "prompt_rewrite": {
            "phase": "提示词改写",
            "step": "分镜 seg_01_04 完成",
            "message": "提示词改写: 分镜 seg_01_04 完成",
            "percent": 44,
            "updated_at": time.time(),
        }
    },
    "updated_at": time.time(),
}

os.makedirs(SESSION_DIR, exist_ok=True)
with open(PATH, "w", encoding="utf-8") as handle:
    json.dump(state, handle, ensure_ascii=False, indent=2)

print(f"[SEED] wrote {PATH}")
print(f"[SEED] session_id={SESSION_ID} items=9 (7 done / 1 failed / 1 pending) "
      f"status[prompt_rewrite]=running percent=44")
