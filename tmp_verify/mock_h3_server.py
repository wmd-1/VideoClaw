# 可复用测试工装：OpenAI 兼容 mock，返回**可通过 H3 校验**的改写正文，用于前端/端到端取证。只用于本地受控测试。
# 能力：按分镜号确定正文长度（第 3 条固定 >8000 字符，验证事件截断与完整正文覆盖）、可配延迟 MOCK_DELAY
#       （使执行跨过多个前端轮询周期）、`/v1/__hits` 请求计数、异常不静默。
#
# 使用三步（缺一不可）：
#   1) 启动：docker cp tmp_verify/mock_h3_server.py video-claw-backend:/tmp/mock_h3_server.py
#            docker exec -d -w /app -e MOCK_DELAY=14 video-claw-backend /app/.venv/bin/python /tmp/mock_h3_server.py
#   2) 零付费安全门（强制，不得只看 .env 文本）：
#      a. 读**运行时生效值** GET :8500/api/config → api_providers.local_llm.base_url，
#         断言 host=127.0.0.1 且 port=18099；不匹配则**立即中止**，不得触发任何阶段执行；
#      b. 走产品自有路径（POST /api/models/test）发一条独立请求，再读 /v1/__hits 确认计数 +1 且
#         errors==0；elapsed_ms 应≈MOCK_DELAY——两者是“路由确实走 mock”的必要证据；
#      c. 测试期间如发现配置偏离或请求流向异常，立即停止。
#   3) 清理：删除取证会话；docker compose up -d --force-recreate --pull never backend 去掉容器内
#      mock 进程与 /tmp 副本；删除 .env 里的 VC_PROVIDER_local_llm__BASE_URL 覆盖；
#      再读一次 /api/config 确认已恢复真实端点。
#
# 安全边界：仅监听容器内回环地址（不对 docker 网络/宿主暴露）；不读写产品配置，
# 不会自行把产品指向任何真实或外部推理端点——“指向 mock”必须由外部显式覆盖并在安全门中验证。
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MOCK_PORT", "18099"))
DELAY = float(os.environ.get("MOCK_DELAY", "2.2"))
N = {"i": 0, "errors": 0}


def body(idx: int) -> str:
    detail = (f"A tabby cat marked scene {idx} crosses a rain-soaked lane at night, slow tracking "
              "shot at eye level, sodium-vapour lamps reflecting on wet asphalt, 50mm shallow depth of field. ")
    # 第 3 条故意给超长正文（> 8000 字符），用于验证事件截断提示与完成后完整正文覆盖
    repeat = 60 if idx == 3 else 2
    return ("```text\n"
            f"integrated_multimodal_description: 8 seconds total. {detail * repeat}\n"
            "overall_soundscape: wet footsteps, distant tram bell, low ambient hum.\n"
            "non_diegetic_music: none.\n"
            "```")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        # 零付费安全门专用：返回请求计数（不记内容，不属 OpenAI 兼容路径）
        if "__hits" in path:
            return self._json_out(200, {"hits": N["i"], "errors": N["errors"], "delay": DELAY})
        payload = json.dumps({"data": [{"id": "mock-h3"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json_out(self, status, obj):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            content = json.loads(raw or b"{}").get("messages", [{}])[-1].get("content", "")
            # 产品的 chat 客户端将 content 构造为多模态 parts 数组，这里统一压成字符串
            prompt = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            # 按分镜号确定正文长度（不用全局调用计数，避免跳次运行累计使第 3 条漏过超长分支）
            match = re.search(r"seg_\d+_(\d+)", prompt) or re.search(r"片段\s*(\d+)", prompt)
            seg_n = int(match.group(1)) if match else 0
            N["i"] += 1
            time.sleep(DELAY)  # 关键：拖长执行期，跨过多个前端轮询周期
            text = f"模式判定：T2VA（无图）\n{body(seg_n)}  <!-- seg={seg_n} call={N['i']} -->"
            out = json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        except Exception as exc:  # noqa: BLE101 - 取证服务不得静默掉请求
            N["errors"] += 1
            self._json_out(500, {"error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    print(f"[mock-h3] listening 127.0.0.1:{PORT} delay={DELAY}s", flush=True)
    # 仅绑定回环：只在后端容器内被访问，不对 docker 网络或宿主暴露
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
