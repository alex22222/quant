# -*- coding: utf-8 -*-
"""控制台静态服务器：转发 npm run dev 的 host/port 参数"""
import json
import sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import os
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)  # 以项目根为 docroot，可访问 status.json / reviews/
sys.path.insert(0, str(ROOT))  # 供 /api/approve 导入 trading_team
from trading_team.state_lock import StateBusy
from trading_team.approvals import ApprovalConflict, ApprovalPending
from trading_team.multica_reviews import ReviewBlocked


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

    def _send_json(self, status, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        """Serve read-only operational APIs and static console assets."""
        route = urlparse(self.path).path
        if route == "/api/multica-reviews":
            from datetime import date
            from trading_team.multica_reviews import review_status
            try:
                day = parse_qs(urlparse(self.path).query).get("date", [""])[0]
                if date.fromisoformat(day).isoformat() != day:
                    raise ValueError("invalid review date")
                self._send_json(200, {"ok": True, "review": review_status(day, team=ROOT / "trading_team")})
            except ValueError:
                self._send_json(400, {"ok": False, "error": "Expected date in YYYY-MM-DD format"})
            return
        if route == "/api/multica-observation":
            from trading_team.multica_observation import observe_multica
            self._send_json(200, {"ok": True, **observe_multica(team=ROOT / "trading_team")})
            return
        if route == "/api/continuous-counterfactual":
            try:
                from trading_team.continuous_counterfactual import current_series
                from trading_team.paper_pipeline import PAPER_AUTO_START
                query = parse_qs(urlparse(self.path).query)
                result = current_series(team=ROOT / "trading_team", start=query.get("start", [PAPER_AUTO_START])[0],
                                        end=query.get("end", [None])[0])
                self._send_json(200, {"ok": True, **result})
            except (OSError, ValueError, TypeError) as e:
                self._send_json(400, {"ok": False, "error": str(e)[:200]})
            return
        if route == "/api/notifications":
            try:
                from trading_team.notification_outbox import summary
                self._send_json(200, {"ok": True, "notifications": summary(team=ROOT / "trading_team")})
            except (OSError, ValueError, TypeError) as e:
                self._send_json(500, {"ok": False, "error": str(e)[:200]})
            return
        if route == "/api/approvals":
            try:
                from trading_team import approvals
                day = parse_qs(urlparse(self.path).query).get("date", [""])[0]
                self._send_json(200, {"ok": True, "approval": approvals.status(day, team=ROOT / "trading_team")})
            except (ValueError, KeyError) as e:
                self._send_json(400, {"ok": False, "error": str(e)[:200]})
            return
        if route == "/api/paper-audit":
            try:
                from trading_team.paper_reconcile import reconcile_accounts
                self._send_json(200, {"ok": True, **reconcile_accounts(team=ROOT / "trading_team")})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)[:200]})
            return
        if route == "/api/backup-service":
            try:
                from trading_team.backup_service import service_status
                from trading_team.backup_launchd import launchd_status
                self._send_json(200, {"ok": True, **service_status(team=ROOT / "trading_team"),
                                      "scheduler": launchd_status(root=ROOT)})
            except (OSError, ValueError, TypeError) as e:
                self._send_json(500, {"ok": False, "error": str(e)[:200]})
            return
        if route == "/api/archive-service":
            try:
                from trading_team.archive_service import service_status
                from trading_team.archive_launchd import launchd_status
                self._send_json(200, {"ok": True, **service_status(team=ROOT / "trading_team"),
                                      "scheduler": launchd_status(root=ROOT)})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)[:200]})
            return
        if route == "/api/operational-snapshot":
            try:
                from trading_team.operational_snapshot import operational_snapshot
                self._send_json(200, {"ok": True, **operational_snapshot(team=ROOT / "trading_team")})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)[:200]})
            return
        if route == "/api/team-db":
            try:
                import sqlite3
                db_path = ROOT / "trading_team" / "team.db"
                if not db_path.exists():
                    self._send_json(404, {"ok": False, "error": "team.db not found"})
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
                    self._send_json(200, {"ok": True, "decisions": decisions, "logs": logs})
                return
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)[:200]})
                return
        if route == "/api/runs" or route.startswith("/api/runs/"):
            try:
                from trading_team.run_ledger import RunLedger
                ledger = RunLedger(readonly=True)
                if route == "/api/runs":
                    self._send_json(200, {"ok": True, "runs": ledger.list_runs()})
                else:
                    run = ledger.get_run(route.removeprefix("/api/runs/"))
                    self._send_json(200 if run else 404, {
                        "ok": bool(run),
                        "run": run,
                        "error": None if run else "run not found",
                    })
                return
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)[:200]})
                return
        if route == "/api/multica-status":
            try:
                from trading_team.multica_adapter import MulticaCLI, load_bootstrap_state
                self._send_json(200, {
                    "ok": True,
                    **MulticaCLI().status().as_dict(),
                    "bootstrap": load_bootstrap_state(),
                })
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)[:200]})
            return
        if route == "/api/paper-state":
            try:
                from trading_team.paper_pipeline import dual_paper_snapshot
                self._send_json(200, {"ok": True, **dual_paper_snapshot()})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)[:200]})
            return
        super().do_GET()

    def do_POST(self):
        """审批写接口：POST /api/approve  {date, code, name, decision, plan_hash, note}"""
        route = urlparse(self.path).path
        if route not in ("/api/approve", "/api/approvals/recover"):
            self.send_error(404)
            return
        from pipeline.policy import require, ResearchOnlyError
        try:
            require("llm_plan")
        except ResearchOnlyError as e:
            self._send_json(403, {"ok": False, "code": "research_only", "error": str(e)})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            from trading_team import approvals
            if route == "/api/approvals/recover":
                result = approvals.recover(str(body["date"]))
                self._send_json(200, {"ok": True, "approval": result})
                return
            entry = approvals.record(
                day=str(body["date"]), code=str(body["code"]),
                name=str(body.get("name", "")), decision=str(body["decision"]),
                plan_hash=str(body["plan_hash"]),
                note=str(body.get("note", ""))[:200], source="console", request_id=body.get("request_id"))
            self._send_json(200, {"ok": True, "entry": entry})
        except StateBusy as e:
            self._send_json(409, {"ok": False, "code": "state_busy", "retryable": True,
                                  "error": str(e)[:200]})
        except ApprovalPending as e:
            self._send_json(503, {"ok": False, "code": "approval_projection_pending", "retryable": True,
                                  "recorded_event_id": e.request_id, "error": str(e)[:200]})
        except ApprovalConflict as e:
            self._send_json(409, {"ok": False, "code": "approval_request_conflict", "retryable": False,
                                  "error": str(e)[:200]})
        except ReviewBlocked as e:
            self._send_json(409, {"ok": False, "code": "review_gate_blocked", "retryable": False,
                                  "error": str(e)[:200]})
        except (ValueError, KeyError, TypeError) as e:
            self._send_json(400, {"ok": False, "error": str(e)[:200]})
        except Exception as e:
            self._send_json(500, {"ok": False, "code": "approval_state_unknown", "error": str(e)[:200]})

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    host, port = parse()
    print(f"量化控制台: http://{host}:{port}/console/")
    ThreadingHTTPServer((host, port), H).serve_forever()
