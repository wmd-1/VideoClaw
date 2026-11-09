#!/usr/bin/env python3
"""会话快照导出 / 回滚 CLI。核心逻辑在 backend/core/session_snapshot.py。

容器内执行前需要把本文件拷进容器（compose 未挂载 tools/）：
  docker cp tools/session_snapshot.py video-claw-backend:/tmp/
  docker exec video-claw-backend /app/.venv/bin/python /tmp/session_snapshot.py \
      export --session-id SID --out /tmp/snapshot --dsn "$VC_DATABASE__DSN"

`/app/core` 已由 compose 挂载，因此导入直接指向容器内的后端目录；
宿主运行时可用 VC_BACKEND 指定 backend 路径。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _backend() -> Path:
    override = os.environ.get("VC_BACKEND")
    if override:
        return Path(override)
    if Path("/app/core").is_dir():
        return Path("/app")
    return Path(__file__).resolve().parent.parent / "video-claw" / "video-claw" / "backend"


sys.path.insert(0, str(_backend()))

from core.session_snapshot import cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(cli())
