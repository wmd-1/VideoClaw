# 清理 seed_rewrite_ui.py 留下的临时取证会话
import os

PATH = "/app/code/data/sessions/zz-ui-rewrite-9items.json"

if os.path.exists(PATH):
    os.remove(PATH)
    print(f"[CLEANUP] removed {PATH}")
else:
    print(f"[CLEANUP] nothing to remove: {PATH} not found")
