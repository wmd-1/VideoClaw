"""沙盒端到端：自定义图像模型对 mock vllm-omni 图像服务的真实 HTTP 调用（openspec 组 6.2）。

覆盖四条路径：文生图（含声明注入）、图生图本地多图、远程混合输入、超限裁剪；
并验证 400 原文透出与临时副本落盘/清理。服务为容器内本地 mock，不产生任何付费调用。
"""
from __future__ import annotations

import base64
import email
import json
import os
import socket
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from config import Config
from models.custom_image import CustomImageClient


def png_bytes(width: int = 64, height: int = 64) -> bytes:
    """构造真实尺寸 PNG（噪声像素，不可压缩），用于体积/形态断言。"""
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


B64_PNG = base64.b64encode(png_bytes(8, 8)).decode()


class MockHandler(BaseHTTPRequestHandler):
    """记录服务端实际收到的字段与 part，返回 OpenAI 图像响应形态。"""

    server_version = "MockVllmOmni/1.0"

    def log_message(self, *args):  # 静音
        pass

    def _record(self, parsed):
        self.server.received.append(parsed)

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        content_type = self.headers.get("content-type", "")
        entry = {"path": self.path, "content_type": content_type}

        if content_type.startswith("application/json"):
            entry["json"] = json.loads(body.decode())
        elif content_type.startswith("multipart/form-data"):
            message = email.message_from_bytes(
                f"MIME-Version: 1.0\r\nContent-Type: {content_type}\r\n\r\n".encode() + body)
            fields: dict[str, list[str]] = {}
            files: list[tuple[str, str]] = []
            for part in message.walk():
                if part.get_content_disposition() != "form-data":
                    continue
                name = part.get_param("name", header="content-disposition") or ""
                filename = part.get_filename()
                if filename:
                    files.append((name, filename))
                else:
                    fields.setdefault(name, []).append(
                        part.get_payload(decode=True).decode("utf-8", "replace"))
            entry["fields"] = fields
            entry["files"] = files
        else:
            entry["raw"] = body[:64].decode("utf-8", "replace")

        self._record(entry)

        if getattr(self.server, "fail_status", None):
            payload = json.dumps({"detail": self.server.fail_detail}).encode()
            self.send_response(self.server.fail_status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        payload = json.dumps({"created": 1, "data": [{"b64_json": B64_PNG}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def mock_server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), MockHandler)
    server.received = []
    server.fail_status = None
    server.fail_detail = ""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def client_for(server, capabilities=None, request_model="Qwen/Qwen-Image-2.1"):
    meta = {
        "id": "e2e-image",
        "request_model": request_model,
        "protocol": "vllm-omni",
        "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
        "api_key": "",
        "capabilities": capabilities or {},
    }
    return CustomImageClient(meta, timeout=30.0)


def write_png(tmp_path, name="ref.png") -> str:
    path = tmp_path / name
    path.write_bytes(png_bytes(32, 32))
    return str(path)


def test_e2e_text_to_image_with_declared_sampling(mock_server, tmp_path):
    client = client_for(mock_server, {"num_inference_steps": "40", "seed": "777"})
    paths = client.generate_image("teapot", save_dir=str(tmp_path),
                                  video_ratio="1:1", resolution="720P")
    assert len(paths) == 1 and os.path.isfile(paths[0])
    entry = mock_server.received[-1]
    assert entry["path"] == "/v1/images/generations"
    assert entry["json"]["num_inference_steps"] == 40
    assert entry["json"]["seed"] == 777
    assert entry["json"]["size"] == "720x720"


def test_e2e_local_multi_reference_as_file_parts(mock_server, tmp_path):
    client = client_for(mock_server, {"max_reference_images": 4})
    paths = [write_png(tmp_path, "a.png"), write_png(tmp_path, "b.png")]
    client.generate_image("combine", image_paths=paths, save_dir=str(tmp_path),
                          video_ratio="1:1", resolution="720P")
    entry = mock_server.received[-1]
    assert entry["path"] == "/v1/images/edits"
    assert entry["content_type"].startswith("multipart/form-data")
    assert [name for name, _ in entry["files"]] == ["image", "image"]
    assert entry["fields"]["model"] == ["Qwen/Qwen-Image-2.1"]
    assert entry["fields"]["size"] == ["720x720"]


def test_e2e_remote_reference_is_downloaded_and_sent_as_file(mock_server, tmp_path, monkeypatch):
    """远程 http 输入：适配层先落盘（本用例拦截下载函数），服务端收到的是文件 part。"""
    client = client_for(mock_server)
    fetched: list[str] = []

    def fake_fetch(url: str):
        fetched.append(url)
        return png_bytes(48, 48), "image/png"

    monkeypatch.setattr(client, "_fetch_remote_bytes", fake_fetch)
    client.generate_image("edit", image_paths=["https://cdn.example/x.png",
                                               write_png(tmp_path, "local.png")],
                          save_dir=str(tmp_path), video_ratio="1:1", resolution="720P")

    assert fetched == ["https://cdn.example/x.png"]
    entry = mock_server.received[-1]
    assert len(entry["files"]) == 2
    assert all(name == "image" for name, _ in entry["files"])
    assert not any("url" in (entry["fields"] or {}) for _ in [1])   # 未使用 url 表单字段


def test_e2e_over_limit_trimmed_before_send(mock_server, tmp_path):
    client = client_for(mock_server, {"max_reference_images": 4})
    paths = [write_png(tmp_path, f"{i}.png") for i in range(6)]
    client.generate_image("combine", image_paths=paths, save_dir=str(tmp_path),
                          video_ratio="1:1", resolution="720P")
    entry = mock_server.received[-1]
    assert len(entry["files"]) == 4


def test_e2e_undeclared_over_limit_surfaces_server_text(mock_server, tmp_path):
    mock_server.fail_status = 400
    mock_server.fail_detail = "too many reference images: expected at most 4"
    client = client_for(mock_server)
    paths = [write_png(tmp_path, f"{i}.png") for i in range(6)]
    with pytest.raises(RuntimeError) as exc:
        client.generate_image("combine", image_paths=paths, save_dir=str(tmp_path),
                              video_ratio="1:1", resolution="720P")
    message = str(exc.value)
    assert "too many reference images" in message        # 服务端原文
    assert "max_reference_images" in message             # 可声明提示
    assert len(mock_server.received[0]["files"]) == 6   # 未声明 → 首次尝试即全量发出
    assert len(mock_server.received) == 3               # 变体链走完（image → image[] → url）


def test_e2e_temp_copies_live_under_temp_dir_and_are_removed(mock_server, tmp_path):
    client = client_for(mock_server)
    created_dirs: list[str] = []
    original = client._refs_cache_dir

    def spy(session_id=None):
        dir_path = original(session_id)
        created_dirs.append(dir_path)
        return dir_path

    client._refs_cache_dir = spy
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes(24, 24)).decode()
    client.generate_image("edit", image_paths=[data_url], save_dir=str(tmp_path),
                          video_ratio="1:1", resolution="720P")

    assert created_dirs
    for dir_path in created_dirs:
        real = os.path.realpath(dir_path)
        assert real.startswith(os.path.realpath(Config.TEMP_DIR) + os.sep)  # 受临时根约束
        assert not os.path.exists(real)                                    # 用完即清理
    assert client._temp_refs == []


def test_e2e_local_paths_are_passed_through_unchanged(mock_server, tmp_path):
    """本地路径不被改写：上游既有的输入路径守卫仍是唯一裁决者。"""
    client = client_for(mock_server)
    local = write_png(tmp_path, "keep.png")
    client.generate_image("edit", image_paths=[local], save_dir=str(tmp_path),
                          video_ratio="1:1", resolution="720P")
    entry = mock_server.received[-1]
    assert entry["files"][0][1] == os.path.basename(local)
    assert client._temp_refs == []
