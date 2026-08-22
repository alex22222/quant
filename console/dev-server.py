#!/usr/bin/env python3
"""
console/dev-server.py —— 开发服务器（自动重载）

用法:
    .venv/bin/python console/dev-server.py           # 默认端口 7100
    .venv/bin/python console/dev-server.py 8080      # 自定义端口

功能:
    - 启动 console/serve.py 作为子进程
    - 监控 console/ 和 trading_team/ 目录下文件变更
    - 文件变更后自动 kill 旧进程、启动新进程
    - 按 Ctrl+C 优雅退出

监控范围:
    - console/*.py, console/*.html, console/*.js, console/*.css
    - trading_team/*.py
"""
import os
import subprocess
import sys
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

ROOT = Path(__file__).resolve().parent.parent
CONSOLE_DIR = ROOT / "console"
TRADING_TEAM_DIR = ROOT / "trading_team"

WATCH_PATTERNS = {".py", ".html", ".js", ".css", ".json"}
IGNORE_PATTERNS = {"__pycache__", ".git", "node_modules", ".venv", "*.swp", "*.tmp"}


def should_reload(path: str) -> bool:
    p = Path(path)
    name = p.name
    for ignore in IGNORE_PATTERNS:
        if ignore in path or (ignore.startswith("*") and name.endswith(ignore[1:])):
            return False
    return p.suffix in WATCH_PATTERNS


class ReloadHandler(FileSystemEventHandler):
    def __init__(self, controller):
        self.controller = controller
        self.last_reload = 0

    def on_any_event(self, event):
        if event.is_directory:
            return
        if not should_reload(event.src_path):
            return
        now = time.time()
        if now - self.last_reload < 0.5:  # debounce
            return
        self.last_reload = now
        print(f"[watch] {event.event_type}: {event.src_path}")
        self.controller.reload()


class DevServer:
    def __init__(self, port: int = 7100):
        self.port = port
        self.proc = None
        self.observer = None

    def start(self):
        print(f"[dev] Console dev server 启动中 (port={self.port})")
        print(f"[dev] 监控目录: {CONSOLE_DIR}, {TRADING_TEAM_DIR}")
        print("[dev] 按 Ctrl+C 退出\n")
        self._spawn()
        self._watch()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[dev] 收到退出信号")
            self.shutdown()

    def _spawn(self):
        if self.proc:
            print("[dev] 终止旧进程...")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, str(CONSOLE_DIR / "serve.py"), str(self.port)],
            cwd=str(ROOT),
            env=env,
        )
        print(f"[dev] serve.py 已启动 (pid={self.proc.pid}) → http://127.0.0.1:{self.port}/console/\n")

    def _watch(self):
        handler = ReloadHandler(self)
        self.observer = Observer()
        self.observer.schedule(handler, str(CONSOLE_DIR), recursive=True)
        self.observer.schedule(handler, str(TRADING_TEAM_DIR), recursive=True)
        self.observer.start()

    def reload(self):
        print("[dev] 代码变更 detected，重启 serve.py...\n")
        self._spawn()

    def shutdown(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        print("[dev] 已退出")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7100
    DevServer(port=port).start()
