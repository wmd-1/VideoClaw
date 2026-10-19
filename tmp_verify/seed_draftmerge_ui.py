# 临时 UI 取证脚本（非产品代码）：为「草稿按内容保留」E2E（draft-merge-preservation 4.1）
# 注入一个处于「等待确认」态的提示词改写会话，含 4 条 done 条目（正文各不相同）。
# waiting 态使阶段整体非 running、textarea 可编辑，从而可复现「后台单条重生成期间编辑另一条目草稿」。
# 用法（项目测试规则：在既有后端容器内执行，不新建镜像/环境）：
#   docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/seed_draftmerge_ui.py
# 之后 docker compose restart backend（快照只读内存，需重载磁盘会话）。
# 遵循资产保留策略：取证会话不删除，作为测试资产保留。
import json
import os
import time

SESSION_ID = "zz-draftmerge-e2e"
SESSION_DIR = "/app/code/data/sessions"
PATH = os.path.join(SESSION_DIR, SESSION_ID + ".json")


def make_item(i: int) -> dict:
    body = (
        f"0.00s-8.00s. Segment {i}: rain-soaked lane at night, eye-level tracking shot, "
        "sodium-vapour reflections on wet asphalt, shallow depth 50mm, distant tram bell, "
        f"warm rim light variant {i}, subject coat hem swings. distinctive tail marker {i}."
    )
    return {
        "id": f"seg_dm_{i:02d}",
        "name": f"取证-片段{i}",
        "index": i,
        "episode": 1,
        "original_prompt": f"分镜 {i}：雨夜小巷，主角背身缓行。",
        "rewritten_prompt": body,
        "input_mode": "I2VA",
        "duration": 8,
        "status": "done",
        "selected": "",
        "versions": [{"content": body[:120], "source": "agent", "created_at": "2026-10-16T10:00:00"}],
        "grounding": {"described": False, "text_only": True},
    }


segments = [
    {
        "segment_id": f"seg_dm_{i:02d}",
        "shots": [{"content": f"镜头{i}：雨夜小巷缓行", "duration": 8}],
        "total_duration": 8,
        "characters": ["主角"],
        "setting": "雨夜后街",
    }
    for i in range(1, 5)
]

state = {
    "session_id": SESSION_ID,
    "current_stage": "prompt_rewrite",
    "status": {
        "script_generation": "completed",
        "character_design": "completed",
        "storyboard": "completed",
        "reference_generation": "completed",
        "prompt_rewrite": "waiting",
        "video_generation": "pending",
        "post_production": "pending",
    },
    "error": None,
    "artifacts": {
        "storyboard": {"episodes": [{"episode_number": 1, "segments": segments}]},
        "prompt_rewrite": {"items": [make_item(i) for i in range(1, 5)]},
    },
    "meta": {
        "idea": "草稿保留 E2E 取证会话（waiting 态 / 4 条 done）",
        "style": "realistic",
        "video_generation_mode": "first_frame",
        "llm_model": "deepseek-v4.1-flash",
        "vlm_model": "local-vlm",
    },
    "stage_progress": {},
    "updated_at": time.time(),
}

os.makedirs(SESSION_DIR, exist_ok=True)
with open(PATH, "w", encoding="utf-8") as handle:
    json.dump(state, handle, ensure_ascii=False, indent=2)

print(f"[SEED] wrote {PATH}")
print(f"[SEED] session_id={SESSION_ID} items=4 (all done) status[prompt_rewrite]=waiting")
