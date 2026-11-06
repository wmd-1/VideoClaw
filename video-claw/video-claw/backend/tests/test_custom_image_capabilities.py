"""图像能力声明的解析、注册表归一与 API 透出（openspec: custom-image-adapter-fidelity 组 1）。

断言分组：
- parse_image_decl：七项合法范围、字符串数值、非法/越界忽略（D6.1 类型表）；
- 注册表归一：非法声明被丢弃、合法声明保留；
- API 透出：/api/models/availability 的 capabilities 含合法声明项；
- 类型隔离：图像声明不进入视频侧解释，反之亦然。
"""
from __future__ import annotations

import logging

from models.config_model import _custom_model_capabilities
from models.custom_common import parse_image_decl

IMAGE_KEYS = ("num_inference_steps", "true_cfg_scale", "seed", "sigmas",
              "max_reference_images", "size_alignment", "turbo")


def test_valid_declarations_are_accepted():
    decl = parse_image_decl({
        "num_inference_steps": 40,
        "true_cfg_scale": 1.0,
        "seed": 42,
        "sigmas": [1.0, 0.9, 0.5],
        "max_reference_images": 4,
        "size_alignment": 32,
        "turbo": True,
    })
    assert decl["num_inference_steps"] == 40
    assert decl["true_cfg_scale"] == 1.0
    assert decl["seed"] == 42
    assert decl["sigmas"] == [1.0, 0.9, 0.5]
    assert decl["max_reference_images"] == 4
    assert decl["size_alignment"] == 32
    assert decl["turbo"] is True


def test_numeric_strings_are_cast_not_rejected():
    """设置页把 capabilities 写成字符串，因此数值字符串必须是合法声明。"""
    decl = parse_image_decl({
        "num_inference_steps": "40",
        "true_cfg_scale": "1.5",
        "seed": "777",
        "max_reference_images": "4",
        "size_alignment": "32",
    })
    assert decl == {
        "num_inference_steps": 40,
        "true_cfg_scale": 1.5,
        "seed": 777,
        "max_reference_images": 4,
        "size_alignment": 32,
    }


def test_out_of_range_values_are_ignored_per_key(caplog):
    cases = {
        "num_inference_steps": (0, "non-positive value"),
        "true_cfg_scale": ("abc", "not a number"),
        "seed": (-1, "negative value"),
        "sigmas": ([0.1, 0.9], "non-increasing"),
        "max_reference_images": ("x", "not an integer"),
        "size_alignment": (24, "power of two"),
    }
    for key, (value, reason) in cases.items():
        with caplog.at_level(logging.WARNING):
            decl = parse_image_decl({key: value})
        assert key not in decl, f"{key}={value!r} must be ignored"
        assert any("capabilities." + key in record.message for record in caplog.records), key
        assert reason in caplog.text or "ignored" in caplog.text


def test_sigmas_rules():
    assert "sigmas" in parse_image_decl({"sigmas": [1.0, 1.0, 0.5]})          # 允许相等（单调不增）
    assert "sigmas" not in parse_image_decl({"sigmas": [0.5, 1.0]})           # 递增非法
    assert "sigmas" not in parse_image_decl({"sigmas": [1.5]})                # 超出 [0,1]
    assert "sigmas" not in parse_image_decl({"sigmas": []})                   # 空列表非法
    assert "sigmas" not in parse_image_decl({"sigmas": "1.0,0.5"})            # 必须是列表


def test_undeclared_model_yields_empty_decl():
    assert parse_image_decl({}) == {}
    assert parse_image_decl(None) == {}
    assert parse_image_decl({"ability_type": "image_generation"}) == {}


def test_registry_keeps_valid_and_drops_invalid_declarations():
    item = {"capabilities": {"num_inference_steps": "40", "size_alignment": 24,
                             "max_reference_images": 4, "turbo": True}}
    caps = _custom_model_capabilities(item, ["t2i", "i2i"])
    assert caps["num_inference_steps"] == 40
    assert caps["max_reference_images"] == 4
    assert caps["turbo"] is True
    assert "size_alignment" not in caps                       # 非 2 的幂被丢弃
    assert caps["ability_type"] == "image_generation"         # 既有标签不受影响


def test_image_declarations_do_not_leak_into_video_capability():
    """图像声明项不参与视频侧 sampling 解释；视频声明项也不被图像侧当作 turbo。"""
    video_caps = _custom_model_capabilities(
        {"capabilities": {"fast_h3": True, "flow_shift": 12}, "abilities": ["text_to_video"]},
        ["video"],
    )
    image_caps = _custom_model_capabilities({"capabilities": {"turbo": True}}, ["t2i"])
    assert video_caps.get("turbo") is None
    assert image_caps.get("fast_h3") is None
    assert image_caps["turbo"] is True


def test_api_models_exposes_declared_image_capabilities(monkeypatch):
    """声明值经注册表透出到 /api/models 的 capabilities（tasks 1.2）。"""
    from config import Config
    from models.config_model import merged_models, model_availability, model_type_capabilities

    monkeypatch.setattr(Config, "CONFIG", {
        "api_providers": {"local_image_t2i": {"protocol": "vllm-omni",
                                              "base_url": "http://127.0.0.1:8091/v1"}},
        "custom_models": [{
            "id": "local-qwen-image-21",
            "provider": "local_image_t2i",
            "model": "Qwen/Qwen-Image-2.1",
            "types": ["t2i", "i2i"],
            "capabilities": {"num_inference_steps": "40", "max_reference_images": "4",
                             "size_alignment": 32, "sigmas": [1.0, 0.5]},
        }],
    }, raising=False)

    assert model_availability("local-qwen-image-21")["available"] is True
    entry = merged_models()["local-qwen-image-21"]
    caps = model_type_capabilities("t2i", entry)
    assert caps["num_inference_steps"] == 40          # 字符串声明已强转
    assert caps["max_reference_images"] == 4
    assert caps["size_alignment"] == 32
    assert caps["sigmas"] == [1.0, 0.5]


def test_api_models_drops_invalid_declarations(monkeypatch):
    """非法声明不进入 API 返回体（等同未声明）。"""
    from config import Config
    from models.config_model import merged_models

    monkeypatch.setattr(Config, "CONFIG", {
        "api_providers": {"local_image_t2i": {"protocol": "vllm-omni",
                                              "base_url": "http://127.0.0.1:8091/v1"}},
        "custom_models": [{
            "id": "local-qwen-image-bad",
            "provider": "local_image_t2i",
            "model": "Qwen/Qwen-Image-2.1",
            "types": ["t2i"],
            "capabilities": {"num_inference_steps": 0, "size_alignment": 24, "seed": -5},
        }],
    }, raising=False)

    caps = merged_models()["local-qwen-image-bad"]["capabilities"]
    assert "num_inference_steps" not in caps
    assert "size_alignment" not in caps
    assert "seed" not in caps


def test_all_seven_keys_are_the_complete_set():
    """完整集合的七项均可声明（规格「图像能力参数声明」）。"""
    decl = parse_image_decl({key: value for key, value in {
        "num_inference_steps": 8, "true_cfg_scale": 1.0, "seed": 0,
        "sigmas": [1.0, 0.0], "max_reference_images": 10, "size_alignment": 16, "turbo": True,
    }.items()})
    assert set(decl) == set(IMAGE_KEYS)


def _example_config() -> dict:
    import os

    import yaml

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml.example")
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_example_templates_register_expected_declarations(monkeypatch):
    """config.yaml.example 的 Qwen-Image-2.1 / Turbo 模板能被注册表正确归一（tasks 1.3）。"""
    from config import Config
    from models.config_model import merged_models

    config_values = _example_config()
    monkeypatch.setattr(Config, "CONFIG", config_values, raising=False)

    entries = merged_models()
    base = entries["local-qwen-image-21"]
    turbo = entries["local-qwen-image-21-turbo"]
    assert base["request_model"] == "Qwen/Qwen-Image-2.1"
    assert base["protocol"] == "vllm-omni"
    assert set(base["type"]) == {"t2i", "i2i"}
    assert base["capabilities"]["num_inference_steps"] == 40
    assert base["capabilities"]["max_reference_images"] == 4
    assert base["capabilities"]["size_alignment"] == 32
    # Turbo 模板不声明步数，只带蒸馏标记
    assert "num_inference_steps" not in turbo["capabilities"]
    assert turbo["capabilities"]["turbo"] is True


def test_example_templates_drive_expected_request_fields(tmp_path, monkeypatch):
    """模板声明直接决定发出字段：基础版带步数并对齐尺寸，Turbo 不带步数也不对齐。"""
    import base64
    import zlib

    from config import Config
    from models.custom_image import CustomImageClient
    from models.config_model import merged_models

    def tiny_png_b64() -> str:
        import struct

        raw = b"\x00\x01\x02\x03"

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw, 0)) + chunk(b"IEND", b""))
        return base64.b64encode(png).decode()

    class PostRecorder:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append({"url": url, **kwargs})
            response = type("Resp", (), {})()
            response.status_code = 200
            response.text = "{}"
            response.headers = {"content-type": "application/json"}
            response.raise_for_status = lambda: None
            response.json = lambda: {"data": [{"b64_json": tiny_png_b64()}]}
            return response

    monkeypatch.setattr(Config, "CONFIG", _example_config(), raising=False)
    recorded = {}
    for key, label in (("local-qwen-image-21", "base"), ("local-qwen-image-21-turbo", "turbo")):
        client = CustomImageClient(dict(merged_models()[key]), timeout=5.0)
        recorder = PostRecorder()
        client._client.post = recorder.post
        client.generate_image("teapot", save_dir=str(tmp_path), video_ratio="1:1", resolution="720P")
        recorded[label] = recorder.calls[0]["json"]

    assert recorded["base"]["num_inference_steps"] == 40
    assert recorded["base"]["true_cfg_scale"] == 1.0
    assert recorded["base"]["size"] == "704x704"          # 声明 size_alignment=32 → 720 向下取整
    assert "num_inference_steps" not in recorded["turbo"]  # Turbo 不下发步数
    assert recorded["turbo"]["true_cfg_scale"] == 1.0
    assert recorded["turbo"]["size"] == "720x720"          # Turbo 模板未声明对齐 → 保现状
