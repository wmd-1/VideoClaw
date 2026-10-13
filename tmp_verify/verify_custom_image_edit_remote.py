"""验证自定义图片模型 i2i 的远程形态分流与回退可达（评审第四轮 I-1）。

不打端点级 mock：直接实例化 CustomImageClient，仅替换其底层 HTTP client 的 post，
断言 `_edit` 对不同输入形态选择正确的表单变体、file 模式失败落入回退而非冒泡。

覆盖：
1. https:// 输入 → 分流到 dataurl/url 表单字段变体（不尝试 open URL）；
2. data: 输入 → 同上分流；
3. 本地文件 → file 变体（multipart 文件句柄）；
4. 不存在的本地路径 → OSError 被捕获、走完变体后以 RuntimeError 终结（不冒泡 FileNotFoundError）；
5. openai 协议 variants 含 url 回退（修复前只有 file 模式）。

运行：docker exec -i video-claw-backend /app/.venv/bin/python - < tmp_verify/verify_custom_image_edit_remote.py
"""
import os
import sys
import tempfile

sys.path.insert(0, "/app")
if os.environ.get("VC_PATCHED_BACKEND"):
    sys.path.insert(0, os.environ["VC_PATCHED_BACKEND"])

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
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=request, response=httpx.Response(self.status_code, request=request))


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


def field_entry(files):
    """提取首个表单项：字段名 + 形态（文件句柄 / 表单字段值）。"""
    if isinstance(files, dict):
        name, entry = next(iter(files.items()))
    elif files:
        name, entry = files[0][0], files[0][1]
    else:
        return None, None
    # entry: (filename, handle/content, mime?) 或 (None, value)
    if isinstance(entry, tuple) and len(entry) >= 2:
        return name, ("file" if entry[0] else ("value", entry[1]))
    return name, ("raw", entry)


# 1) https:// 输入分流到 dataurl/url 表单字段（不 open URL）
for protocol in ("vllm-omni", "sglang", "openai"):
    client, calls = make_client(protocol)
    items = client._edit("p", ["https://example.com/a.png"], "512x512")
    name, shape = field_entry(calls[0]["files"]) if calls else (None, None)
    check(f"{protocol}: https 输入走表单字段变体（非文件句柄）",
          len(calls) == 1 and shape == ("value", "https://example.com/a.png"),
          f"calls={len(calls)} field={name} shape={shape!r}")

# 2) data: 输入同上分流
client, calls = make_client("vllm-omni")
client._edit("p", ["data:image/png;base64,QgE="], "512x512")
name, shape = field_entry(calls[0]["files"]) if calls else (None, None)
check("vllm-omni: data: 输入走表单字段变体",
      len(calls) == 1 and shape == ("value", "data:image/png;base64,QgE="),
      f"calls={len(calls)} shape={shape!r}")

# 3) 本地文件走 file 变体（multipart 文件句柄）
with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
    tmp.write(b"\x89PNG\r\n\x1a\n")
    local = tmp.name
try:
    client, calls = make_client("vllm-omni")
    client._edit("p", [local], "512x512")
    name, shape = field_entry(calls[0]["files"]) if calls else (None, None)
    check("vllm-omni: 本地文件走文件句柄变体",
          len(calls) == 1 and shape == "file" and name == "image",
          f"calls={len(calls)} field={name} shape={shape!r}")

    # 4) 不存在的本地路径：所有变体 open 失败均被捕获，无网络请求发出，以 RuntimeError 终结
    client, calls = make_client("vllm-omni")
    missing = f"/nonexistent_{os.getpid()}/nope.png"
    try:
        client._edit("p", [missing], "512x512")
        raised = None
    except RuntimeError as exc:
        raised = f"RuntimeError: {str(exc)[:60]}"
    except OSError:
        raised = "OSError(冒泡)"
    check("本地路径不存在时以 RuntimeError 终结而非 OSError 冒泡",
          raised is not None and raised.startswith("RuntimeError") and len(calls) == 0,
          f"raised={raised} calls={len(calls)}")

    # 5) file 变体请求被服务器拒绝（400）时，回退变体真实可达（修复前异常冒泡终结/回退不可达）
    client400, calls400 = make_client("sglang", status=400)
    try:
        client400._edit("p", [local], "512x512")
        raised400 = "no-exception"
    except RuntimeError:
        raised400 = "RuntimeError"
    name2, shape2 = field_entry(calls400[1]["files"]) if len(calls400) > 1 else (None, None)
    check("file 变体被 400 拒绝后 url 回退可达",
          raised400 == "RuntimeError" and len(calls400) == 2 and name2 == "url",
          f"raised={raised400} calls={len(calls400)} second_field={name2} shape={shape2!r}")
finally:
    os.unlink(local)

# 6) openai 协议 variants 含 url 回退
variants = make_client("openai")[0]._edit_field_variants()
check("openai 协议含 url(dataurl) 回退变体",
      ("url", "dataurl") in variants, f"variants={variants}")

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
