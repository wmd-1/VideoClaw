"""验证图生图多图/混合输入不再静默丢弃（openspec: custom-adapter-remote-inputs D5）。

只替换底层 HTTP client 的 post，断言实际发出的字段值与数量：
1. 远程+远程 → 两个同名 url 值；
2. 远程+本地 → 同一字段两个值（远程原样、本地转 data URL）；
3. 本地+本地 → file 模式两个文件 part；
4. 单图 → 字段名/文件名/MIME 与改动前一致；
5. 多值被 400 拒绝 → 不退化只发首图。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_custom_image_multi_input.py
"""
import os
import sys
import tempfile

sys.path.insert(0, "/app")

from models.custom_image import CustomImageClient  # noqa: E402

failures = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        failures.append(name)


class FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code
        self.text = '{"data":[{"b64_json":"aGk="}]}'

    def json(self):
        return {"data": [{"b64_json": "aGk="}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", "http://127.0.0.1:9/images/edits")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=request,
                response=httpx.Response(self.status_code, request=request),
            )


def make_client(protocol, status=200):
    client = CustomImageClient({
        "id": "m1", "request_model": "m1", "protocol": protocol,
        "base_url": "http://127.0.0.1:9", "api_key": "k",
    })
    calls = []
    client._client.post = lambda url, data=None, files=None: (  # type: ignore[method-assign]
        calls.append({"url": url, "data": dict(data or {}), "files": files}) or FakeResponse(status)
    )
    return client, calls


def parts_of(files):
    pairs = list(files.items()) if isinstance(files, dict) else list(files or [])
    parts = []
    for name, entry in pairs:
        if isinstance(entry, tuple):
            filename = entry[0]
            content = entry[1] if len(entry) > 1 else None
            mime = entry[2] if len(entry) > 2 else None
        else:
            filename, content, mime = None, entry, None
        parts.append((name, filename, content, mime))
    return parts


# 1) 远程 + 远程：两个同名 url 值
client, calls = make_client("vllm-omni")
url1, url2 = "https://example.com/a.png", "https://example.com/b.png"
client._edit("p", [url1, url2], "512x512")
parts = parts_of(calls[0]["files"]) if calls else []
check("远程多图：请求只发一次且携带两个值", len(calls) == 1 and len(parts) == 2, f"calls={len(calls)} parts={len(parts)}")
check("远程多图：字段名为 url 且无文件名", bool(parts) and all(p[0] == "url" and p[1] is None for p in parts), str([(p[0], p[1]) for p in parts]))
check("远程多图：两个值均原样保留", [p[2] for p in parts] == [url1, url2], str([p[2] for p in parts]))

# 2) 远程 + 本地：同一字段两个值，本地转 data URL
with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
    tmp.write(b"\x89PNG\r\n\x1a\n")
    local = tmp.name
try:
    client, calls = make_client("sglang")
    client._edit("p", [url1, local], "512x512")
    parts = parts_of(calls[0]["files"]) if calls else []
    check("混合输入：两个值均送达", len(parts) == 2, f"parts={len(parts)}")
    check("混合输入：远程 URL 原样保留", bool(parts) and parts[0][2] == url1, str(parts[0][2] if parts else None))
    check("混合输入：本地文件转 data URL", bool(parts) and str(parts[1][2]).startswith("data:image/png;base64,"), str(parts[1][2])[:40] if len(parts) > 1 else "")

    # 3) 本地 + 本地：file 模式两个文件 part
    client, calls = make_client("openai")
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp2:
        tmp2.write(b"\xff\xd8\xff")
        local2 = tmp2.name
    try:
        client._edit("p", [local, local2], "512x512")
        parts = parts_of(calls[0]["files"]) if calls else []
        check("本地多图：file 模式发送两个文件 part", len(parts) == 2 and all(p[1] for p in parts), f"parts={len(parts)}")
    finally:
        os.unlink(local2)

    # 4) 单图回归：字段名、文件名、MIME 不变
    client, calls = make_client("sglang")
    client._edit("p", [local], "512x512")
    parts = parts_of(calls[0]["files"]) if calls else []
    check("单图：字段名保持 image", len(parts) == 1 and parts[0][0] == "image", str(parts[:1]))
    check("单图：文件名保持 basename", bool(parts) and parts[0][1] == os.path.basename(local), str(parts[:1]))
    check("单图：MIME 保持 image/png", bool(parts) and parts[0][3] == "image/png", str(parts[:1]))
finally:
    os.unlink(local)

# 5) 多值被 400 拒绝：不退化只发首图
client, calls = make_client("openai", status=400)
error = None
try:
    client._edit("p", [url1, url2], "512x512")
except RuntimeError as exc:
    error = str(exc)
check("多值 400：以 RuntimeError 终结而非静默成功", error is not None and "400" in str(error), str(error)[:120])
check("多值 400：没有发出只含首图的重试", all(len(parts_of(call["files"])) != 1 for call in calls), str([len(parts_of(c["files"])) for c in calls]))

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL PASS")
