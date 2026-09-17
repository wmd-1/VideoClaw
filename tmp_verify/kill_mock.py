"""终止容器内旧 mock_server 进程（按 /proc cmdline 匹配）。"""
import glob
import os
import signal
import sys

killed = []
for path in glob.glob("/proc/[0-9]*/cmdline"):
    try:
        cmd = open(path, "rb").read().decode(errors="ignore")
    except Exception:
        continue
    if "mock_server.py" in cmd:
        try:
            pid = int(path.split("/")[2])
        except (ValueError, IndexError):
            continue
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except ProcessLookupError:
            pass
print("KILLED", killed)
sys.exit(0)