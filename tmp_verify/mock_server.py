"""独立 mock 推理服务（在既有 Docker 容器内常驻运行，供 API 级验证使用）。

启动：docker exec -d -w /app video-claw-backend /app/.venv/bin/python /tmp/mock_server.py
环境变量：MOCK_PORT（默认 18099）、MOCK_VIDEO_POLLS（默认 2）
"""
import base64
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MOCK_PORT", "18099"))
VIDEO_POLLS = int(os.environ.get("MOCK_VIDEO_POLLS", "2"))
PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"MOCK-MP4-" + b"x" * 64

RECORDS = []
JOBS = {}
JOB_SEQ = {"n": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, status, payload, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _record(self, body=b"", extra=None):
        entry = {
            "method": self.command,
            "path": self.path,
            "content_type": self.headers.get("Content-Type", ""),
            "authorization": self.headers.get("Authorization"),
            "field_names": sorted({n.decode() for n in re.findall(rb'name="([^"]+)"', body)}),
        }
        if extra:
            entry.update(extra)
        RECORDS.append(entry)

    def do_POST(self):
        body = self._read_body()
        path = self.path.rstrip("/")
        if path == "/v1/chat/completions":
            self._record(body)
            data = json.loads(body)
            content = data["messages"][0]["content"]
            has_image = any(p.get("type") == "image_url" for p in content)
            return self._json(200, {"choices": [{"message": {"content": f"MOCK-OK img={int(has_image)}"}}]})
        if path == "/v1/images/generations":
            self._record(body)
            return self._json(200, {"data": [{"b64_json": PNG_1X1_B64}]})
        if path == "/v1/images/edits":
            names = {n.decode() for n in re.findall(rb'name="([^"]+)"', body)}
            self._record(body)
            if names & {"image", "image[]", "url"}:
                return self._json(200, {"data": [{"b64_json": PNG_1X1_B64}]})
            return self._json(400, {"detail": f"unexpected fields {sorted(names)}"})
        if path == "/v1/videos":
            self._record(body)
            JOB_SEQ["n"] += 1
            job_id = f"mock-job-{JOB_SEQ['n']}"
            JOBS[job_id] = {"polls": 0}
            return self._json(200, {"id": job_id, "status": "queued"})
        if path == "/__reset":
            RECORDS.clear()
            JOBS.clear()
            return self._json(200, {"ok": True})
        return self._json(404, {"detail": f"no route {path}"})

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        # 管理端点不计入记录（避免自引用干扰计数）
        if path == "/__records":
            return self._json(200, {"records": RECORDS})
        if path == "/__health":
            return self._json(200, {"ok": True})
        self._record()
        if path.startswith("/v1/videos/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4] == "content":
                return self._bytes(200, MP4_BYTES, "video/mp4")
            job_id = parts[3]
            job = JOBS.get(job_id)
            if not job:
                return self._json(404, {"detail": "job not found"})
            job["polls"] += 1
            status = "completed" if job["polls"] >= VIDEO_POLLS else "in_progress"
            payload = {"id": job_id, "status": status}
            if status == "completed":
                payload["url"] = f"/v1/videos/{job_id}/content"
            return self._json(200, payload)
        if path == "/v1/videos":
            return self._json(200, {"data": [{"id": j, "status": "in_progress"} for j in JOBS]})
        if path.startswith("/v1/images/") and path.endswith("/content"):
            return self._bytes(200, base64.b64decode(PNG_1X1_B64), "image/png")
        return self._json(404, {"detail": f"no route {path}"})

    def do_DELETE(self):
        self._record()
        return self._json(200, {"deleted": True})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()