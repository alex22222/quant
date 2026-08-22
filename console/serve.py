# -*- coding: utf-8 -*-
"""控制台静态服务器：转发 npm run dev 的 host/port 参数"""
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import os

os.chdir(Path(__file__).parent.parent)  # 以项目根为 docroot，可访问 status.json / reviews/


def parse():
    host, port = "127.0.0.1", 7100
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a in ("--port", "-p") and i + 1 < len(args):
            port = int(args[i + 1])
        elif a in ("--host",) and i + 1 < len(args):
            host = args[i + 1]
        elif a.isdigit():
            port = int(a)
    return host, port


class H(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    host, port = parse()
    print(f"量化控制台: http://{host}:{port}/console/")
    ThreadingHTTPServer((host, port), H).serve_forever()
