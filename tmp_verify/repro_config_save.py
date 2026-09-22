"""复现"新增自定义模型保存后消失"：三组场景走真实 PUT 保存链路（结束自动恢复备份并提示重启）。"""
import copy
import json
import shutil
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "/app")
import yaml  # noqa: E402
from config import CONFIG_PATH  # noqa: E402

shutil.copy(CONFIG_PATH, "/tmp/cfg_repro_backup.yaml")
BASE = "http://127.0.0.1:8500"  # 容器内外同端口（.env BACKEND_PORT）


def req(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())


with CONFIG_PATH.open("r", encoding="utf-8") as f:
    raw0 = yaml.safe_load(f)
m3 = [m for m in raw0.get("custom_models", []) if m.get("id") == "m3"]
print("== 现存 m3 条目字段:", json.dumps(m3[0] if m3 else None, ensure_ascii=False))


def try_add(entry, label):
    current = req("GET", "/api/config")["config"]
    payload = copy.deepcopy(current)
    payload["custom_models"] = [m for m in payload["custom_models"] if m.get("id") != entry["id"]]
    payload["custom_models"].append(entry)
    try:
        resp = req("PUT", "/api/config", {"values": payload})
        ids = [m["id"] for m in resp["config"].get("custom_models", [])]
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            file_ids = [m.get("id") for m in (yaml.safe_load(f) or {}).get("custom_models", [])]
        print(f"== {label}: 保存成功 | 响应 ids={ids} | 文件 ids={file_ids} | 含目标={entry['id'] in ids}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:220]
        print(f"== {label}: HTTP {e.code} | {body}")


try_add({"id": "repro-a", "provider": "local_video_vllm", "model": "Wan", "types": ["video"], "abilities": ["text_to_video"], "concurrency": 1}, "A) types 非空的正常新增")
try_add({"id": "repro-b", "provider": "local_video_vllm", "model": "Wan", "types": [], "concurrency": 1}, "B) types 为空的新增")
try_add({"id": "repro-c", "provider": "local_video_vllm", "model": "Wan", "types": ["video"], "abilities": [], "concurrency": 1}, "C) abilities 为空的新增")

shutil.copy("/tmp/cfg_repro_backup.yaml", CONFIG_PATH)
print("== 已恢复配置文件备份（请重启后端以回退内存状态）")