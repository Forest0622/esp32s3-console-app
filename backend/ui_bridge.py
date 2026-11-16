"""本地 HTTP 桥（文档 4 / 5 章 UI-Bridge）。

只监听 127.0.0.1，默认端口 8765；被占用时在 8765–8784 内顺延。
画面走 /api/frame.bmp：用无压缩 BMP 直传，避免引入图像编码依赖。
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import subprocess

import firmware_backup_manager
import project_importer

APP_ROOT = Path(__file__).resolve().parent.parent
FRONTEND = Path(os.environ.get("CONSOLE_APP_FRONTEND")
                or APP_ROOT / "frontend-ui")
BASE_PORT = 8765

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def frame_to_bmp(fb: bytes, width: int, height: int) -> bytes:
    """RGB565(小端) → 24 位 BMP（BGR、自下而上）。"""
    row_bytes = width * 3
    pad = (4 - row_bytes % 4) % 4
    stride = row_bytes + pad
    pixels = bytearray()
    for y in range(height - 1, -1, -1):
        base = y * width * 2
        row = bytearray()
        for x in range(width):
            lo = fb[base + x * 2]
            hi = fb[base + x * 2 + 1]
            v = (hi << 8) | lo
            r = ((v >> 11) & 0x1F) * 8
            g = ((v >> 5) & 0x3F) * 4
            b = (v & 0x1F) * 8
            row += bytes((b, g, r))
        row += b"\x00" * pad
        pixels += row

    header = b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0,
                       len(pixels), 2835, 2835, 0, 0)
    return header + info + bytes(pixels)


class Handler(BaseHTTPRequestHandler):
    app = None            # 由 serve() 注入
    server_version = "ConsoleApp/1.0"

    def log_message(self, fmt, *args):   # 静音访问日志，避免污染应用日志
        return

    # ---------------------------------------------------------- 工具

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return {}

    # ---------------------------------------------------------- 路由

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        app = self.app

        if path == "/api/state":
            self._json({"ok": True, "state": app.snapshot()})
            return
        if path == "/api/ports":
            self._json({"ok": True, "ports": app.scan_ports()})
            return
        if path == "/api/adapters":
            self._json({"ok": True,
                        "adapters": project_importer.load_adapters(verified_only=True)})
            return
        if path == "/api/frame.bmp":
            fb = app.latest_frame()
            if fb is None:
                self._json({"ok": False, "error": "no_frame"}, 404)
                return
            bmp = frame_to_bmp(fb, app.parser.width, app.parser.height)
            self._send(200, bmp, "image/bmp")
            return

        # 静态文件
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (FRONTEND / rel).resolve()
        if not str(target).startswith(str(FRONTEND.resolve())) or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._send(200, target.read_bytes(),
                   MIME.get(target.suffix.lower(), "application/octet-stream"))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        app = self.app
        body = self._body()

        try:
            if path == "/api/scan":
                self._json({"ok": True, "ports": app.scan_ports()})
            elif path == "/api/connect":
                self._json(app.connect(body.get("port") or body.get("device") or ""))
            elif path == "/api/disconnect":
                self._json(app.disconnect())
            elif path == "/api/key":
                self._json(app.send_key(int(body.get("idx", 0))))
            elif path == "/api/import":
                p = project_importer.import_project(body.get("path", ""))
                self._json({"ok": p.ok, "project": p.to_dict()})
            elif path == "/api/backup":
                # 必须把进度回调接上，否则界面进度恒为 0（实测：3 分钟仍是「准备备份」）
                full = bool(body.get("full", False))
                self._json(self._run_async("backup", lambda: app.start_backup(
                    progress_cb=lambda frac, text: app.set(
                        progress=frac, progress_text=text), full=full)))
            elif path == "/api/rollback":
                self._json(self._run_async("rollback", app.rollback))
            elif path == "/api/open-backups":
                # 访达小图标：直接 open 备份目录，开发态与打包态都走这里
                try:
                    firmware_backup_manager.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                    subprocess.run(["open", str(firmware_backup_manager.BACKUP_DIR)],
                                   check=False)
                    self._json({"ok": True})
                except OSError as exc:
                    self._json({"ok": False, "error": str(exc)})
            elif path == "/api/stop":
                self._json(app.stop())
            elif path == "/api/deploy":
                self._json(self._run_async("deploy", lambda: app.deploy(
                    body.get("source", ""), body.get("components", []),
                    body.get("adapters", []), body.get("mode", "inject"))))
            else:
                self._json({"ok": False, "error": "unknown endpoint"}, 404)
        except (ValueError, OSError) as exc:
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def _run_async(self, kind: str, fn) -> dict:
        """长任务放到后台线程，接口立刻返回，进度靠 /api/state 轮询。"""
        def runner() -> None:
            try:
                res = fn()
            except Exception as exc:  # noqa: BLE001
                self.app.set(status="error", busy="", error=f"{type(exc).__name__}: {exc}")
                self.app.log(f"{kind} 异常：{exc}")
                return
            # 闸门类失败（NO_BACKUP / NO_CHIP / 编译失败…）过去只 return 一个 dict，
            # 异步线程里没人接，界面上就是「点了没反应」。这里统一落到 state.error。
            if isinstance(res, dict) and not res.get("ok", True):
                code = res.get("code") or ""
                text = res.get("error") or "任务失败"
                if text.startswith("已停止"):
                    # 用户主动停止不是故障：不要把它记成 ERROR，避免界面一片红
                    self.app.set(busy="", progress=0.0, error="",
                                 progress_text=text)
                    self.app.log(f"{kind} 已停止：{text}")
                else:
                    self.app.set(status="error", busy="", error=text,
                                 hint=res.get("hint") or "")
                    self.app.log(f"{kind} 被拦下（{code or 'ERROR'}）：{text}")

        threading.Thread(target=runner, name=f"task-{kind}", daemon=True).start()
        return {"ok": True, "started": kind}


def _free_port(start: int = BASE_PORT, tries: int = 20) -> int:
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("没有可用端口")


def serve(app, port: int | None = None) -> int:
    port = port or _free_port()
    Handler.app = app
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    app.log(f"本地界面：http://127.0.0.1:{port}")
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    return port
