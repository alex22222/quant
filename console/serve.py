# -*- coding: utf-8 -*-
"""控制台静态服务器：转发 npm run dev 的 host/port 参数"""
import json
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)  # 以项目根为 docroot，可访问 status.json / reviews/
sys.path.insert(0, str(ROOT))  # 供 /api/approve 导入 trading_team


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

    def do_GET(self):
        """GET /api/team-db 返回 team.db 决策状态与执行日志"""
        if self.path == "/api/team-db":
            try:
                import sqlite3
                db_path = ROOT / "trading_team" / "team.db"
                if not db_path.exists():
                    payload = json.dumps({"ok": False, "error": "team.db not found"}).encode("utf-8")
                    self.send_response(404)
                else:
                    conn = sqlite3.connect(db_path)
                    conn.row_factory = sqlite3.Row
                    decisions = [dict(r) for r in conn.execute(
                        "SELECT date, pm_status, executed_at, execution_result FROM decisions ORDER BY date DESC"
                    )]
                    logs = [dict(r) for r in conn.execute(
                        "SELECT decision_date, code, side, qty, price, amount, reason, executed_at FROM execution_log ORDER BY id DESC"
                    )]
                    conn.close()
                    payload = json.dumps({"ok": True, "decisions": decisions, "logs": logs},
                                         ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            except Exception as e:
                payload = json.dumps({"ok": False, "error": str(e)[:200]}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
        super().do_GET()
    def log_message(self, *a):
        pass

    def do_POST(self):
        """审批写接口：POST /api/approve  {date, code, name, decision, note}"""
        if self.path != "/api/approve":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            from trading_team import approvals
            entry = approvals.record(
                day=str(body["date"]), code=str(body["code"]),
                name=str(body.get("name", "")), decision=str(body["decision"]),
                note=str(body.get("note", ""))[:200], source="console")
            payload = json.dumps({"ok": True, "entry": entry},
                                 ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        except Exception as e:
            payload = json.dumps({"ok": False, "error": str(e)[:200]},
                                 ensure_ascii=False).encode("utf-8")
            self.send_response(400)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    host, port = parse()
    print(f"量化控制台: http://{host}:{port}/console/")
    ThreadingHTTPServer((host, port), H).serve_forever()
