"""Read-only, project-scoped remote evidence; connectivity is not completion."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import UUID

from trading_team.multica_adapter import MulticaCLI, _entity

TEAM = Path(__file__).resolve().parent
READ_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError,
               sqlite3.Error, subprocess.SubprocessError)


def _unavailable(exc: Exception) -> dict:
    return {"status": "unavailable", "error": f"Evidence unavailable ({type(exc).__name__})"}


def _failure_reason(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid failure reason")
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict) and isinstance(parsed.get("detail"), str):
            value = parsed["detail"]
    except ValueError:
        pass
    return re.sub(r"(?i)\b(?:sk-[\w-]+|Bearer\s+[^\s\"']+)", "[redacted]", value)[:1000]


def _local_attempt(team: Path, latest: dict | None, state: dict) -> dict:
    if not latest or not latest.get("issue_id"):
        return {"status": "missing"}
    path = team / ".runs/ledger.db"
    if not path.exists():
        return {"status": "missing"}
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT a.*,w.business_date,w.status AS run_status,w.plan_hash FROM workflow_attempts a "
                "JOIN workflow_runs w ON a.run_id=w.run_id WHERE w.workflow='daily_research' "
                "ORDER BY a.started_at DESC,a.rowid DESC")
            for row in rows:
                context = json.loads(row["external_context_json"])
                if (context.get("provider") == "multica" and context.get("verification") == "remote_verified"
                        and context.get("project_id") == state["project_id"]
                        and context.get("issue_id") == latest["issue_id"]
                        and (not latest.get("task_id") or context.get("task_id") == latest["task_id"])):
                    return {"status": "linked", "run_id": row["run_id"], "attempt": row["attempt"],
                            "attempt_status": row["status"], "run_status": row["run_status"],
                            "business_date": row["business_date"], "plan_hash": row["plan_hash"],
                            "task_id": context.get("task_id")}
    except READ_ERRORS as exc:
        return _unavailable(exc)
    return {"status": "missing"}


def observe_multica(*, team: Path = TEAM, cli: MulticaCLI | None = None) -> dict:
    result = {"schema": "multica_observation.v1", "observed_at": datetime.now().astimezone().isoformat(),
              "status": "unavailable", "completion_verified": False,
              "configuration": {"status": "unavailable"}, "history": {"status": "unavailable"},
              "runtime": {"status": "unavailable"}, "local_attempt": {"status": "missing"}}
    state_path = team / ".runs/multica/bootstrap.json"
    if not state_path.exists():
        return {**result, "status": "unconfigured"}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        for key in ("project_id", "autopilot_id", "leader_agent_id", "runtime_id", "daemon_id", "trigger_id"):
            UUID(state[key])
        root = team.parent / "multica"
        expected = json.loads((root / "workspace.json").read_text(encoding="utf-8"))["autopilot"]
        runbook = (team.parent / "docs/MULTICA_PRODUCTION_RUNBOOK.md").read_text(encoding="utf-8")
        cli = cli or MulticaCLI(cwd=Path(tempfile.gettempdir()))
        payload = cli._run_json(["autopilot", "get", state["autopilot_id"], "--output", "json"], timeout=8)
        auto = _entity(payload, "autopilot")
        if (auto["id"] != state["autopilot_id"] or auto.get("project_id") != state["project_id"]
                or auto.get("assignee_type") != "agent" or auto.get("assignee_id") != state["leader_agent_id"]):
            return {**result, "status": "mismatch", "configuration": {"status": "mismatch"}}
        triggers = payload["triggers"]
        if not isinstance(triggers, list) or any(not isinstance(item, dict) for item in triggers):
            raise ValueError("invalid triggers")
        differences = []
        if auto.get("description") != runbook:
            differences.append("runbook")
        if auto.get("issue_title_template") != expected["issue_title_template"]:
            differences.append("issue_title_template")
        if auto.get("status") != "active":
            differences.append("autopilot_inactive")
        if len(triggers) != 1 or any(
            item.get("id") != state["trigger_id"] or item.get("autopilot_id") != state["autopilot_id"]
            or item.get("kind") != "schedule" or item.get("enabled") is not True
            or item.get("cron_expression") != expected["cron"] or item.get("timezone") != expected["timezone"]
            for item in triggers
        ):
            differences.append("trigger")
        digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
        result["configuration"] = {
            "status": "drift" if differences else "matched", "differences": differences,
            "autopilot_id": auto["id"], "project_id": state["project_id"], "agent_id": state["leader_agent_id"],
            "remote_status": auto.get("status"), "local_runbook_hash": digest(runbook),
            "remote_runbook_hash": digest(auto["description"]) if isinstance(auto.get("description"), str) else None,
            "triggers": [{key: item.get(key) for key in ("id", "kind", "enabled", "cron_expression", "timezone",
                                                        "last_fired_at", "next_run_at")} for item in triggers],
        }
    except READ_ERRORS as exc:
        result["configuration"] = _unavailable(exc)
        return result

    try:
        payload = cli._run_json(["autopilot", "runs", state["autopilot_id"], "--limit", "5", "--output", "json"], timeout=8)
        runs, total = payload["runs"], payload["total"]
        if (not isinstance(runs, list) or type(total) is not int or total < len(runs)
                or (not runs and total != 0)):
            raise ValueError("invalid run history")
        items = []
        for run in runs:
            if (run["autopilot_id"] != state["autopilot_id"] or not run["id"]
                    or not isinstance(run["status"], str)):
                raise ValueError("invalid run binding")
            timestamp = datetime.fromisoformat(run["triggered_at"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError("run time has no timezone")
            item = {key: run.get(key) for key in ("id", "status", "source", "triggered_at", "completed_at", "issue_id", "task_id")}
            item["failure_reason"] = _failure_reason(run.get("failure_reason"))
            items.append((timestamp, item))
        items = [item for _, item in sorted(items, key=lambda pair: pair[0], reverse=True)]
        result["history"] = {"status": "available" if items else "empty", "total": total,
                             "limit": 5, "items": items, "latest": items[0] if items else None}
    except READ_ERRORS as exc:
        result["history"] = _unavailable(exc)

    try:
        agent = _entity(cli._run_json(["agent", "get", state["leader_agent_id"], "--output", "json"], timeout=8), "agent")
        if agent["id"] != state["leader_agent_id"] or agent.get("runtime_id") != state["runtime_id"]:
            result["runtime"] = {"status": "mismatch"}
        else:
            runtimes = cli._run_json(["runtime", "list", "--output", "json"], timeout=8)
            if not isinstance(runtimes, list) or any(not isinstance(item, dict) for item in runtimes):
                raise ValueError("invalid runtime list")
            matches = [item for item in runtimes if item.get("id") == state["runtime_id"]]
            if not matches:
                result["runtime"] = {"status": "missing", "id": state["runtime_id"]}
            elif len(matches) != 1 or matches[0].get("daemon_id") != state["daemon_id"]:
                result["runtime"] = {"status": "mismatch"}
            else:
                runtime = matches[0]
                result["runtime"] = {"status": "matched", "id": runtime["id"], "daemon_id": runtime["daemon_id"],
                    "remote_status": runtime.get("status"), "provider": runtime.get("provider"),
                    "version": runtime.get("metadata", {}).get("version"), "model": agent.get("model") or None,
                    "last_seen_at": runtime.get("last_seen_at")}
    except READ_ERRORS as exc:
        result["runtime"] = _unavailable(exc)

    latest = result["history"].get("latest")
    result["local_attempt"] = _local_attempt(team, latest, state)
    result["status"] = "observed" if (
        result["configuration"]["status"] == "matched" and result["history"]["status"] == "available"
        and latest["status"] == "succeeded" and result["runtime"]["status"] == "matched"
        and result["runtime"]["remote_status"] == "online" and result["local_attempt"]["status"] == "linked"
        and result["local_attempt"]["attempt_status"] == result["local_attempt"]["run_status"] == "succeeded"
    ) else "attention"
    return result
