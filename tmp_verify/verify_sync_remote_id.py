"""验证 vllm-omni sync 直出的远端标识（M2，评审加固）。

覆盖：
- _sync_remote_identifier 对已落盘文件返回 `sync://<16位sha1>`（不以 http 开头）；
- 文件缺失时退回 `sync://<basename>`（仍可解释、非伪 URL）；
- generate_video 走 sync 直出成功路径返回的标识为 sync://，不再是 {base_url}/videos/sync。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_sync_remote_id.py
"""
import os
import sys
import tempfile

sys.path.insert(0, "/app")
if os.environ.get("VC_PATCHED_BACKEND"):
    sys.path.insert(0, os.environ["VC_PATCHED_BACKEND"])

from models.custom_video import CustomVideoClient  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


meta = {
    "id": "h3", "request_model": "MiniMax-H3",
    "base_url": "http://127.0.0.1:1/v1", "protocol": "vllm-omni", "api_key": "",
}
client = CustomVideoClient(meta)

# 1) 已落盘文件：内容摘要标识
sample = os.path.join(tempfile.gettempdir(), "vc_sync_sample.mp4")
with open(sample, "wb") as f:
    f.write(b"\x00\x00\x00\x18ftypmp42" + b"z" * 100)
ident = CustomVideoClient._sync_remote_identifier(sample)
check("sync 标识为 sync://<摘要>", ident.startswith("sync://") and not ident.startswith("http"), ident)
check("标识含 16 位十六进制摘要", len(ident.split("://", 1)[1]) == 16, ident)

# 相同内容 → 相同摘要；不同内容 → 不同摘要
sample2 = sample + ".copy"
with open(sample2, "wb") as f:
    f.write(b"\x00\x00\x00\x18ftypmp42" + b"z" * 100)
check("相同内容摘要一致", CustomVideoClient._sync_remote_identifier(sample2) == ident)
sample3 = sample + ".diff"
with open(sample3, "wb") as f:
    f.write(b"different-bytes")
check("不同内容摘要不同", CustomVideoClient._sync_remote_identifier(sample3) != ident, CustomVideoClient._sync_remote_identifier(sample3))

# 2) 文件缺失：退回 basename（非伪 URL）
missing = os.path.join(tempfile.gettempdir(), "vc_missing_video_xyz.mp4")
if os.path.exists(missing):
    os.remove(missing)
fallback = CustomVideoClient._sync_remote_identifier(missing)
check("缺失文件退回 sync://<basename>", fallback == "sync://vc_missing_video_xyz.mp4", fallback)

# 3) generate_video sync 成功路径返回 sync://（不再返回 base_url/videos/sync）
def fake_try_sync(**kwargs):
    save_path = kwargs["save_path"]
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    with open(save_path, "wb") as fh:
        fh.write(b"\x00\x00\x00\x18ftypmp42sync")
    return None  # 成功信号（原实现返回 None 表示无需回退异步）

client._try_sync_generate = fake_try_sync
out_path = os.path.join(tempfile.gettempdir(), "vc_e2e_sync_out.mp4")
result = client.generate_video(prompt="p", image_path="", save_path=out_path, model="h3", duration=5)
check("generate_video sync 直出返回 sync:// 标识", str(result).startswith("sync://") and "/videos/sync" not in str(result), f"result={result}")

for p in (sample, sample2, sample3, out_path):
    if os.path.exists(p):
        os.remove(p)

print(f"\n{'ALL PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(0 if not failures else 1)
