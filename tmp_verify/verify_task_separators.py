"""tasks 分隔符前后端共享用例（openspec: review-fifth-minor-followups M-1）。

运行（容器内，夹具需先复制到 /tmp）：
docker cp tmp_verify/fixtures/tasks_separators.json video-claw-backend:/tmp/tasks_separators.json
docker exec -i -w /app -e PYTHONPATH=/app video-claw-backend \
  /app/.venv/bin/python - < tmp_verify/verify_task_separators.py
"""
import json
import os
import sys

sys.path.insert(0, "/app")

from models.custom_common import parse_task_list  # noqa: E402
from models.custom_video import CustomVideoClient  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


fixture_path = os.environ.get("VC_TASK_FIXTURE", "/tmp/tasks_separators.json")
with open(fixture_path, "r", encoding="utf-8") as handle:
    fixture = json.load(handle)

allowed = {"t2va", "fl2va", "ref2va"}
for case in fixture["cases"]:
    parsed = parse_task_list(case["input"])
    check(f"parse_task_list {case['name']}", parsed == case["expected"], str(parsed))
    invalid = [token for token in parsed if token not in allowed]
    check(f"非法 token 判定 {case['name']}", invalid == case["invalid"], str(invalid))

client = CustomVideoClient({
    "id": "verify-tasks",
    "request_model": "verify-tasks",
    "protocol": "sglang",
    "base_url": "http://127.0.0.1:9",
    "api_key": "",
    "capabilities": {"tasks": "t2va，fl2va|bogus"},
})
try:
    check("_supported_tasks 复用共享解析并过滤枚举", client._supported_tasks() == ["t2va", "fl2va"], str(client._supported_tasks()))
finally:
    client._client.close()

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL PASS")
