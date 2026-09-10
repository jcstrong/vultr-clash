#!/usr/bin/env python3
"""订阅服务器 (HTTP, 默认经 Tailscale 可达)."""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLASH_YAML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clash.yaml")
PORT = int(os.environ.get("SUB_PORT", "8888"))
PUB_HOST = os.environ.get("PUB_HOST", "localhost")

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path == "/sub/clash.yaml" or self.path == "/":
            if os.path.exists(CLASH_YAML):
                self.send_response(200)
                self.send_header("Content-Type", "text/yaml; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with open(CLASH_YAML, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"no active node, create one first")
        elif self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

def main():
    print(f"Sub server: http://0.0.0.0:{PORT}/sub/clash.yaml")
    print(f"Public:     http://{PUB_HOST}:{PORT}/sub/clash.yaml")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
