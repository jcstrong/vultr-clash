#!/usr/bin/env python3
"""订阅服务器 (默认经 Tailscale 私有分发, 跨平台 Python 标准库).

订阅鉴权:
  - 存在 .state/sub_token 时启用: 仅 /sub/{token}/clash.yaml 可访问, 其余一律 404(不泄露存在性)
  - 不存在该文件时向后兼容: /sub/clash.yaml 直接可订阅(开箱即用)
"""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLASH_YAML = os.path.join(BASE_DIR, "clash.yaml")
STATE_DIR = os.path.join(BASE_DIR, ".state")
TOKEN_FILE = os.path.join(STATE_DIR, "sub_token")

PORT = int(os.environ.get("SUB_PORT", "8888"))
PUB_HOST = os.environ.get("PUB_HOST", "localhost")
CLASH_PATH = os.environ.get("SUB_PATH", "/sub/clash.yaml").lstrip("/")
UPDATE_INTERVAL = os.environ.get("SUB_UPDATE_INTERVAL", "14400")
TITLE = os.environ.get("SUB_TITLE", "vultr-clash")


def read_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        route = self.path.split("?", 1)[0].lstrip("/")

        if route == "health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return

        token = read_token()

        if route == CLASH_PATH:
            # 未启用鉴权 (无 token 文件) 时保留旧路径直接订阅
            if not token:
                self._serve_yaml()
            else:
                self._err(404)
            return

        if route.startswith("sub/") and token:
            parts = route.split("/")
            if len(parts) >= 3 and parts[1] == token and "/".join(parts[2:]) == CLASH_PATH.split("/", 1)[-1]:
                self._serve_yaml()
                return

        self._err(404)

    def _serve_yaml(self):
        if not os.path.exists(CLASH_YAML):
            self._err(404, b"no active node, create one first")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/yaml; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Profile-Update-Interval", UPDATE_INTERVAL)
        self.send_header("Profile-Title", TITLE)
        self.end_headers()
        with open(CLASH_YAML, "rb") as f:
            self.wfile.write(f.read())

    def _err(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    token = read_token()
    if not token:
        link = f"/{CLASH_PATH}"
    else:
        link = f"/sub/{token}/{CLASH_PATH.split('/', 1)[-1]}"
    print(f"Sub server: http://0.0.0.0:{PORT}{link}")
    print(f"Public:     http://{PUB_HOST}:{PORT}{link}")
    print("订阅鉴权: 已启用 (需带 token)  |  未启用 (无 .state/sub_token)" if token
          else "订阅鉴权: 未启用 (保持 /sub/clash.yaml)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()