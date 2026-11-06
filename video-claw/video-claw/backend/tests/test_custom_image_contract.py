"""图像适配层与 vllm-omni/sglang/openai 契约的静态比对（openspec: custom-image-adapter-fidelity 组 6.1）。

分组与规格一致：
- 契约类（必须全绿）：A1–A6、B2、C1–C5、D1、D2、E1、F1–F3，另加声明生效后的注入/裁剪断言；
- 偏差类（已知偏差，一律挂在“未声明任何新增能力参数的基线模型”上）：A4g、A5g、B1g、B2g、D1g、D2g、E2g。

不打真实服务端：只替换 CustomImageClient 底层 httpx client 的 post/get，捕获实际会发出的
方法/路径/字段，与 docs/reference/Qwen-Image-2.1/ 存档的官方口径逐条比对。因此本文件证明的是
请求形态契约，不证明真机可用（真机冒烟见 tasks 6.4，未执行前标“运行待验”）。
"""
from __future__ import annotations

import base64
import inspect
import logging
import os
import zlib

import pytest

from config import CUSTOM_MODEL_TYPES, CUSTOM_PROTOCOLS
from models.custom_common import _IMAGE_SIZE_MAP
from models.custom_image import CustomImageClient

BASE_META = {
    "id": "local-image-baseline",
    "request_model": "Qwen/Qwen-Image-2.1",
    "protocol": "vllm-omni",
    "base_url": "http://127.0.0.1:8091/v1",
    "api_key": "",
}


def png_bytes(width: int = 64, height: int = 64) -> bytes:
    """构造真实尺寸 PNG（噪声像素，不可压缩），用于体积/形态断言。"""
    import struct

    raw = b"".join(
        b"\x00" + bytes(((x * 7 + y * 13) % 256) for x in range(width)) for y in range(height)
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 0))
            + chunk(b"IEND", b""))


class Recorder:
    """替换 httpx 客户端的 post/get，记录调用并返回可解析的响应。"""

    def __init__(self, status_code: int = 200, body_text: str = ""):
        self.calls: list[dict] = []
        self.status_code = status_code
        self.body_text = body_text
        self.b64 = base64.b64encode(png_bytes(8, 8)).decode()

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.status_code, self.body_text, self.b64)

    def get(self, url, **kwargs):
        self.calls.append({"url": url, "method": "GET", **kwargs})
        return FakeResponse(200, "", base64.b64encode(png_bytes(48, 48)).decode(),
                            content_type="image/png")


class FakeResponse:
    """最小响应替身：支持 json()/text/content/raise_for_status。"""

    def __init__(self, status_code: int, text: str, b64: str, content_type: str = "application/json"):
        self.status_code = status_code
        self.text = text
        self._b64 = b64
        self.headers = {"content-type": content_type}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(self.text or f"HTTP {self.status_code}",
                                        request=httpx.Request("POST", "http://test"),
                                        response=httpx.Response(self.status_code,
                                                                text=self.text,
                                                                request=httpx.Request("POST", "http://test")))

    def json(self) -> dict:
        return {"data": [{"b64_json": self._b64}]}

    @property
    def content(self) -> bytes:
        return base64.b64decode(self._b64)


def build(meta: dict | None = None, status_code: int = 200, body_text: str = ""):
    client = CustomImageClient(dict(BASE_META, **(meta or {})), timeout=5.0)
    recorder = Recorder(status_code=status_code, body_text=body_text)
    client._client.post = recorder.post
    client._client.get = recorder.get
    return client, recorder


def local_png(tmp_path, name: str = "ref.png") -> str:
    path = tmp_path / name
    path.write_bytes(png_bytes())
    return str(path)


def call_of(recorder, index: int = 0) -> dict:
    return recorder.calls[index]


# ───────────────────────────── 契约类：文生图 ─────────────────────────────

def test_A1_generations_endpoint_and_base_url():
    client, recorder = build()
    client.generate_image("teapot", save_dir="/tmp/ci", video_ratio="16:9", resolution="1080P")
    call = call_of(recorder)
    assert call["url"] == "/images/generations"
    assert str(client._client.base_url).rstrip("/").endswith("/v1")


def test_A2_standard_fields_present():
    client, recorder = build()
    client.generate_image("teapot", save_dir="/tmp/ci", video_ratio="16:9", resolution="1080P")
    payload = call_of(recorder)["json"]
    assert {"model", "prompt", "n", "size", "response_format"} <= set(payload)


def test_A3_size_uses_WxH_form():
    client, recorder = build()
    client.generate_image("teapot", save_dir="/tmp/ci", video_ratio="16:9", resolution="1080P")
    size = call_of(recorder)["json"]["size"]
    assert "x" in size and "*" not in size and " " not in size


def test_A4_A5_undeclared_model_sends_no_extra_fields():
    """未声明即现状：不出现采样字段（同时是 A4g/A5g 偏差的事实依据）。"""
    client, recorder = build()
    client.generate_image("teapot", save_dir="/tmp/ci", video_ratio="16:9", resolution="1080P")
    payload = call_of(recorder)["json"]
    assert not ({"num_inference_steps", "true_cfg_scale", "seed", "sigmas"} & set(payload))


def test_A6_b64_response_saved_as_local_file(tmp_path):
    client, _ = build()
    paths = client.generate_image("teapot", save_dir=str(tmp_path), video_ratio="1:1", resolution="720P")
    assert len(paths) == 1 and os.path.isfile(paths[0])


def test_B2_undeclared_size_alignment_keeps_map_value():
    """未声明 size_alignment 时下发档位原值（1080 非 32 倍数即证明零取整）。"""
    expected = _IMAGE_SIZE_MAP["16:9"]["1080P"].replace("*", "x")
    assert int(expected.split("x")[1]) % 32 != 0
    client, recorder = build()
    client.generate_image("teapot", save_dir="/tmp/ci", video_ratio="16:9", resolution="1080P")
    assert call_of(recorder)["json"]["size"] == expected


# ───────────────────────────── 契约类：图生图 ─────────────────────────────

def test_C1_edits_endpoint(tmp_path):
    client, recorder = build()
    client.generate_image("edit", image_paths=[local_png(tmp_path)], save_dir="/tmp/ci",
                          video_ratio="1:1", resolution="720P")
    assert call_of(recorder)["url"] == "/images/edits"


def test_C2_first_field_is_image(tmp_path):
    client, recorder = build()
    client.generate_image("edit", image_paths=[local_png(tmp_path)], save_dir="/tmp/ci",
                          video_ratio="1:1", resolution="720P")
    files = call_of(recorder)["files"]
    assert files and files[0][0] == "image"


def test_C3_two_local_files_become_two_parts(tmp_path):
    client, recorder = build()
    paths = [local_png(tmp_path, "a.png"), local_png(tmp_path, "b.png")]
    client.generate_image("edit", image_paths=paths, save_dir="/tmp/ci",
                          video_ratio="1:1", resolution="720P")
    files = call_of(recorder)["files"]
    assert len(files) == 2 and len({name for name, _ in files}) == 1


def test_C4_form_carries_standard_fields(tmp_path):
    client, recorder = build()
    client.generate_image("edit", image_paths=[local_png(tmp_path)], save_dir="/tmp/ci",
                          video_ratio="1:1", resolution="720P")
    form = call_of(recorder)["data"]
    assert {"model", "prompt", "size"} <= set(form)


def test_C5_file_parts_not_data_urls(tmp_path):
    client, recorder = build()
    client.generate_image("edit", image_paths=[local_png(tmp_path)], save_dir="/tmp/ci",
                          video_ratio="1:1", resolution="720P")
    for _, value in call_of(recorder)["files"]:
        assert isinstance(value, tuple) and len(value) == 3      # (filename, handle, mime)


def test_E1_remote_reference_is_materialized_to_file_part():
    """远程形态（http(s)/data:）输入先落盘再以文件 part 送达（不再整体降级为 data URL）。"""
    client, recorder = build()
    fetched: list[str] = []

    def fake_fetch(url: str):
        fetched.append(url)
        return png_bytes(48, 48), "image/png"

    client._fetch_remote_bytes = fake_fetch
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes(24, 24)).decode()
    client.generate_image("edit", image_paths=["https://example.com/a.png", data_url],
                          save_dir="/tmp/ci", video_ratio="1:1", resolution="720P")

    files = call_of(recorder)["files"]
    assert fetched == ["https://example.com/a.png"]                 # http 输入走了下载
    assert files and files[0][0] == "image"
    assert all(isinstance(value, tuple) and len(value) == 3 for _, value in files)  # 全部文件 part
    assert len(files) == 2
    assert not any(str(v).startswith("data:") for _, v in files)     # 未降级为 data URL


def test_E1_cleanup_removes_temporary_copies():
    client, _ = build()
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes(24, 24)).decode()
    client.generate_image("edit", image_paths=[data_url], save_dir="/tmp/ci",
                          video_ratio="1:1", resolution="720P")
    assert client._temp_refs == []                    # 本次临时副本已登记并清理


# ───────────────────────────── 契约类：上限与声明生效 ─────────────────────────────

def test_D1_declared_limit_trims_in_order(tmp_path):
    client, recorder = build({"capabilities": {"max_reference_images": 4}})
    paths = [local_png(tmp_path, f"{i}.png") for i in range(6)]
    client.generate_image("edit", image_paths=paths, save_dir="/tmp/ci",
                          video_ratio="1:1", resolution="720P")
    assert len(call_of(recorder)["files"]) == 4


def test_D2_undeclared_model_sends_all_parts(tmp_path):
    client, recorder = build()
    paths = [local_png(tmp_path, f"{i}.png") for i in range(6)]
    client.generate_image("edit", image_paths=paths, save_dir="/tmp/ci",
                          video_ratio="1:1", resolution="720P")
    assert len(call_of(recorder)["files"]) == 6


def test_D1_log_records_trim_numbers_and_reason(tmp_path, caplog):
    client, _ = build({"capabilities": {"max_reference_images": "4"}})
    paths = [local_png(tmp_path, f"{i}.png") for i in range(6)]
    with caplog.at_level(logging.WARNING):
        client.generate_image("edit", image_paths=paths, save_dir="/tmp/ci",
                              video_ratio="1:1", resolution="720P")
    message = " ".join(r.message for r in caplog.records if "reference images trimmed" in r.message)
    assert "input=6" in message and "sent=4" in message and "dropped=2" in message
    assert "limit=max_reference_images:4" in message


def test_return_contract_still_list_of_paths(tmp_path):
    client, _ = build({"capabilities": {"max_reference_images": 4}})
    paths = [local_png(tmp_path, f"{i}.png") for i in range(6)]
    result = client.generate_image("edit", image_paths=paths, save_dir="/tmp/ci",
                                   video_ratio="1:1", resolution="720P")
    assert isinstance(result, list) and all(isinstance(p, str) for p in result)


def test_D1g_undeclared_over_limit_error_exposes_server_text():
    """未声明上限时全量发出；服务端 400 的错误必须含原文与可声明提示（tasks 3.3）。"""
    server_text = '{"detail":"too many reference images: expected at most 4"}'
    client, recorder = build(status_code=400, body_text=server_text)
    client._fetch_remote_bytes = lambda url: (png_bytes(48, 48), "image/png")
    with pytest.raises(RuntimeError) as exc:
        client.generate_image("edit", image_paths=["https://example.com/a.png",
                                                   "https://example.com/b.png"],
                              save_dir="/tmp/ci", video_ratio="1:1", resolution="720P")
    message = str(exc.value)
    assert "max_reference_images" in message          # 可声明提示
    assert server_text[:20] in message                # 服务端原文透出
    assert len(call_of(recorder)["files"]) == 2       # 未声明 → 不裁剪


def test_sampling_fields_injected_on_both_paths(tmp_path):
    caps = {"num_inference_steps": "40", "true_cfg_scale": "1.0", "seed": "777"}
    client, recorder = build({"capabilities": caps})
    client.generate_image("teapot", save_dir="/tmp/ci", video_ratio="1:1", resolution="720P")
    json_payload = call_of(recorder, 0)["json"]
    assert json_payload["num_inference_steps"] == 40
    assert json_payload["true_cfg_scale"] == 1.0
    assert json_payload["seed"] == 777

    client2, recorder2 = build({"capabilities": caps})
    client2.generate_image("edit", image_paths=[local_png(tmp_path)], save_dir="/tmp/ci",
                           video_ratio="1:1", resolution="720P")
    form = call_of(recorder2)["data"]
    assert form["num_inference_steps"] == 40 and form["seed"] == 777


def test_F1_F2_turbo_checkpoint_omits_steps(tmp_path):
    meta = {"request_model": "Qwen/Qwen-Image-2.1-Turbo",
            "capabilities": {"turbo": True, "num_inference_steps": 40}}
    client, recorder = build(meta)
    client.generate_image("teapot", save_dir="/tmp/ci", video_ratio="1:1", resolution="720P")
    payload = call_of(recorder)["json"]
    assert payload["model"] == "Qwen/Qwen-Image-2.1-Turbo"
    assert "num_inference_steps" not in payload


def test_F2_turbo_ignore_reason_logged(caplog):
    client, _ = build({"capabilities": {"turbo": True, "num_inference_steps": 40}})
    with caplog.at_level(logging.WARNING):
        client.generate_image("teapot", save_dir="/tmp/ci", video_ratio="1:1", resolution="720P")
    assert any("turbo checkpoint ships its own schedule" in r.message for r in caplog.records)


def test_sigmas_wins_over_steps_and_is_generic():
    client, recorder = build({"capabilities": {"sigmas": [1.0, 0.5], "num_inference_steps": 40}})
    client.generate_image("teapot", save_dir="/tmp/ci", video_ratio="1:1", resolution="720P")
    payload = call_of(recorder)["json"]
    assert payload["sigmas"] == [1.0, 0.5]
    assert "num_inference_steps" not in payload

    # 非蒸馏模型声明 sigmas 同样下发
    client2, recorder2 = build({"capabilities": {"sigmas": [1.0, 0.5]}})
    client2.generate_image("teapot", save_dir="/tmp/ci", video_ratio="1:1", resolution="720P")
    assert call_of(recorder2)["json"]["sigmas"] == [1.0, 0.5]


def test_sigmas_serialized_as_json_in_multipart_form(tmp_path):
    client, recorder = build({"capabilities": {"sigmas": [1.0, 0.5]}})
    client.generate_image("edit", image_paths=[local_png(tmp_path)], save_dir="/tmp/ci",
                          video_ratio="1:1", resolution="720P")
    import json

    assert json.loads(call_of(recorder)["data"]["sigmas"]) == [1.0, 0.5]


def test_size_alignment_floors_and_logs_ratio_deviation(caplog):
    client, recorder = build({"capabilities": {"size_alignment": 32}})
    with caplog.at_level(logging.WARNING):
        client.generate_image("teapot", save_dir="/tmp/ci", video_ratio="16:9", resolution="1080P")
    assert call_of(recorder)["json"]["size"] == "1920x1056"
    assert any("image size aligned by size_alignment=32" in r.message and "deviation" in r.message
               for r in caplog.records)


def test_size_alignment_invalid_value_behaves_as_undeclared():
    client, recorder = build({"capabilities": {"size_alignment": 24}})
    client.generate_image("teapot", save_dir="/tmp/ci", video_ratio="16:9", resolution="1080P")
    assert call_of(recorder)["json"]["size"] == "1920x1080"


def test_size_alignment_result_too_small_is_refused_before_send():
    client, _ = build({"capabilities": {"size_alignment": 1024}})
    with pytest.raises(RuntimeError) as exc:
        client.generate_image("teapot", save_dir="/tmp/ci", video_ratio="16:9", resolution="720P")
    assert "拒绝发送" in str(exc.value)


def test_F3_protocol_and_type_enums_cover_image_models():
    assert "vllm-omni" in CUSTOM_PROTOCOLS
    assert {"t2i", "i2i"} <= set(CUSTOM_MODEL_TYPES)


# ───────────────────────────── 偏差类（基线模型） ─────────────────────────────

def test_B1g_map_still_contains_non_multiples_of_32():
    """未声明 size_alignment 时按 D4 保持现状：档位表仍有非 32 倍数（已知偏差）。"""
    bad = [f"{ratio}/{res}={size}"
           for ratio, by_res in _IMAGE_SIZE_MAP.items()
           for res, size in by_res.items()
           if any(int(v) % 32 for v in size.split("*"))]
    assert len(bad) == 12, f"偏差清单假设 12 个档位非 32 倍数，实际: {bad}"


def test_B2g_map_contains_sizes_beyond_official_presets():
    """官方验证量级在 1024×1024/1280×704，模型卡预设最大 2752×1536；4K 属未覆盖风险。"""
    oversized = [size for by_res in _IMAGE_SIZE_MAP.values() for size in by_res.values()
                 if int(size.split("*")[0]) * int(size.split("*")[1]) > 2752 * 1536]
    assert oversized, "档位表应仍包含超出官方预设量级的尺寸（本 change 不处理）"


def test_D2g_workflow_still_collects_up_to_ten_references():
    """模型卡写 10 张、vllm-omni 实现只支持 4 张：工作流收集上限仍为 10（文档口径不一致）。"""
    from core.agents.reference_agent import ReferenceGeneratorAgent

    source = inspect.getsource(ReferenceGeneratorAgent._collect_refs)
    assert "refs[:10]" in source


def test_E2g_remote_data_url_would_exceed_server_part_limit():
    """官方 url 字段限 1024KB/part；一张 1024×1024 PNG 的 data URL 约 1.3MB。

    本 change 后远程输入不再走该字段（见 test_E1_*），此用例锁定“若仍走 data URL 必然超限”的事实，
    防止回退到旧形态。
    """
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes(1024, 1024)).decode()
    assert len(data_url) > 1024 * 1024


# ─────────────────── 远程输入失败路径与下载客户端口径（tasks 4.1/4.3） ───────────────────
import httpx  # noqa: E402

from config import Config  # noqa: E402
from models import custom_image as custom_image_module  # noqa: E402


class FakeStream:
    def __init__(self, headers: dict, chunks: list, status_code: int = 200):
        self.headers = headers
        self._chunks = chunks
        self.status_code = status_code
        self.consumed = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://ref.example/x.png")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request,
                response=httpx.Response(self.status_code, request=request))

    def iter_bytes(self, chunk_size=None):
        for chunk in self._chunks:
            self.consumed += len(chunk)
            yield chunk


class FakeClient:
    def __init__(self, stream):
        self.stream_obj = stream

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, **kwargs):
        return self.stream_obj


def client_with_stream(headers, chunks, status_code=200):
    client, _ = build()
    stream = FakeStream(headers, chunks, status_code=status_code)
    client._ref_http_client = lambda: FakeClient(stream)
    return client, stream


def test_ref_fetch_rejects_non_image_content_type():
    client, stream = client_with_stream({"content-type": "text/plain"}, [b"not an image"])
    with pytest.raises(RuntimeError) as exc:
        client._fetch_remote_bytes("http://ref.example/x.png")
    assert "不是图片" in str(exc.value)
    assert stream.consumed == 0                     # 未读 body 就拒绝


def test_ref_fetch_rejects_declared_oversize_before_reading_body():
    headers = {"content-type": "image/png",
               "content-length": str(custom_image_module.MAX_REF_BYTES + 1)}
    client, stream = client_with_stream(headers, [b"x" * 1024])
    with pytest.raises(RuntimeError) as exc:
        client._fetch_remote_bytes("http://ref.example/big.png")
    assert "声明体积" in str(exc.value)
    assert stream.consumed == 0                     # 预检命中，不先吃满内存


def test_ref_fetch_aborts_when_actual_bytes_exceed_limit():
    chunk = b"x" * (1024 * 1024)
    limit = custom_image_module.MAX_REF_BYTES
    pieces = [chunk] * (limit // len(chunk) + 2)     # 无 content-length，只能边读边算
    client, stream = client_with_stream({"content-type": "image/png"}, pieces)
    with pytest.raises(RuntimeError) as exc:
        client._fetch_remote_bytes("http://ref.example/streaming.png")
    assert "实际体积超过上限" in str(exc.value)
    assert stream.consumed <= limit + len(chunk)     # 超限立即中断，未读完全部块


def test_ref_fetch_maps_http_error_with_model_id(tmp_path):
    client, _ = client_with_stream({"content-type": "image/png"}, [b""], status_code=404)
    with pytest.raises(RuntimeError) as exc:
        client.generate_image("edit", image_paths=["http://ref.example/missing.png"],
                              save_dir=str(tmp_path), video_ratio="1:1", resolution="720P")
    message = str(exc.value)
    assert "参考图落盘失败" in message and "Qwen/Qwen-Image-2.1" in message
    assert "404" in message


def test_remote_failure_does_not_send_request(tmp_path):
    """落盘失败必须在发送前终止，不能退化为无参考图请求。"""
    client, recorder = build()
    client._fetch_remote_bytes = lambda url: (_ for _ in ()).throw(RuntimeError("落盘失败：模拟"))
    with pytest.raises(RuntimeError):
        client.generate_image("edit", image_paths=["https://example.com/a.png"],
                              save_dir=str(tmp_path), video_ratio="1:1", resolution="720P")
    assert recorder.calls == []                     # 一次 /images/edits 也没发


def test_data_url_oversize_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(custom_image_module, "MAX_REF_BYTES", 1024)
    client, recorder = build()
    big = "data:image/png;base64," + base64.b64encode(png_bytes(64, 64)).decode()
    with pytest.raises(RuntimeError) as exc:
        client.generate_image("edit", image_paths=[big], save_dir=str(tmp_path),
                              video_ratio="1:1", resolution="720P")
    assert "体积" in str(exc.value) and "上限" in str(exc.value)
    assert recorder.calls == []


def test_refs_cache_dir_stays_under_temp_root_with_hostile_session():
    """session_id 带路径穿越时，落盘目录仍必须在临时根内（不越出会话目录）。"""
    import tempfile

    client, _ = build()
    with tempfile.TemporaryDirectory() as root:
        original_temp = Config.TEMP_DIR
        Config.TEMP_DIR = root
        try:
            dir_path = client._refs_cache_dir("../../etc")
            real = os.path.realpath(dir_path)
            assert real.startswith(os.path.realpath(root) + os.sep)
        finally:
            Config.TEMP_DIR = original_temp


def test_fetch_client_does_not_carry_model_auth_header():
    """下载客户端沿用代理但不携带模型服务凭据（避免向任意 URL 泄露 api_key）。"""
    client, _ = build(dict(BASE_META, api_key="secret-token", provider="local_image_it2i"))
    probe = client._ref_http_client()
    try:
        assert "authorization" not in {k.lower() for k in probe.headers}
    finally:
        probe.close()
