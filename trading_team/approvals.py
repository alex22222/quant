# -*- coding: utf-8 -*-
"""老板批复台账：trading_team/loops/approvals.json

批复来源：console 审批面板（POST /api/approve）或 Kimi 对话（agent 代记）。

事实源规则（改进计划 Phase 0）：
- approvals.json 只是**审批操作日志**，不再是判断依据；
- 唯一主事实源是 decisions/YYYY-MM-DD/decision.json；
- record() 每次批复都会把结果**回写 decision.json** 的 per-plan approval /
  executable / blocked_reason，并重算 pm_status；console/飞书只读主事实源；
- market_ok=false 时买入计划即使批复 approved，executable 也强制为 false（fail closed）。
"""
from __future__ import annotations

import json
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
import re
import sqlite3
import uuid

try:
    from trading_team.schemas import compute_executable
except ImportError:  # 直接以脚本方式运行（cwd 不在项目根）时退化为本地导入
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from trading_team.schemas import compute_executable

from trading_team.state_lock import atomic_write_json, atomic_write_text, state_lock
from trading_team.decision_pipeline import canonical_plan_hash
from trading_team.approval_audit import (
    EVENT_SCHEMA, PROJECTION_SCHEMA, REQUEST_FIELDS, database_event, entry_for,
    event_hash, inspect_projection, marker, projected_entries, validate_history,
    validate_decision_facts,
)

TEAM = Path(__file__).resolve().parent
FILE = TEAM / "loops" / "approvals.json"
DECISIONS_DIR = TEAM / "decisions"
VALID = ("approved", "rejected", "reduced")


class ApprovalConflict(ValueError):
    pass


class ApprovalPending(RuntimeError):
    def __init__(self, request_id: str, error: Exception):
        self.request_id = request_id
        super().__init__(f"approval recorded; projection recovery required: {error}")


def load() -> dict:
    if FILE.exists():
        return json.loads(FILE.read_text(encoding="utf-8"))
    return {"decisions": []}


def save(data: dict) -> None:
    atomic_write_json(FILE, data)


def record(day: str, code: str, name: str, decision: str, plan_hash: str,
           note: str = "", source: str = "console", *, request_id: str | None = None) -> dict:
    from pipeline.policy import require
    require("llm_plan")
    with state_lock(TEAM):
        return _record(day, code, name, decision, plan_hash, note, source, request_id)


def _record(day: str, code: str, name: str, decision: str, plan_hash: str,
            note: str, source: str, request_id: str | None, decided_at: str | None = None) -> dict:
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("invalid approval date")
    if decision not in VALID:
        raise ValueError(f"decision 必须是 {VALID}")
    if not isinstance(plan_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", plan_hash):
        raise ValueError("必须提供 plan_hash")
    request_id = request_id if request_id is not None else uuid.uuid4().hex
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
        raise ValueError("invalid approval request_id")
    requested = dict(date=day, code=str(code), name=name, decision=decision,
                     plan_hash=plan_hash, note=note, source=source)
    if not all(isinstance(value, str) for value in requested.values()):
        raise ValueError("approval request fields must be strings")
    path = _decision_path(day)
    if not path.exists():
        raise ValueError(f"主事实源不存在: {path}")
    stored = json.loads(path.read_text(encoding="utf-8"))
    events = validate_history(stored)
    existing = next((event for event in events if event["event_id"] == request_id), None)
    if existing:
        if any(existing[key] != requested[key] for key in REQUEST_FIELDS):
            raise ApprovalConflict("approval request_id already binds different content")
        synced = _recover_or_pending(stored)
        return entry_for(existing, synced) | {"projection_status": "complete"}
    master_doc = _checked_decision(day, plan_hash)
    if not any(str(p.get("code")) == str(code) for p in master_doc.get("plans", [])):
        raise ValueError(f"当前计划不存在标的 {code}")
    if decision != "rejected":
        from trading_team.multica_reviews import require_reviews
        require_reviews(day, plan_hash, team=TEAM)
    if events:
        master_doc = _recover_or_pending(master_doc)
    event = requested | {
        "schema": EVENT_SCHEMA, "event_id": request_id,
        "decided_at": decided_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "previous_event_hash": events[-1]["event_hash"] if events else None,
    }
    event["event_hash"] = event_hash(event)
    _apply_approval(master_doc, event)
    master_doc["approval_events"] = events + [event]
    destinations = master_doc.get("approval_projection", {}).get("destinations", {})
    master_doc["approval_projection"] = {
        "schema": PROJECTION_SCHEMA, "status": "pending", "through_event_hash": event["event_hash"],
        "destinations": destinations,
    }
    validate_history(master_doc)
    try:
        atomic_write_json(path, master_doc)
    except OSError as exc:
        # A directory fsync can fail after replacement; never report that as a rejected intent.
        persisted = json.loads(path.read_text(encoding="utf-8"))
        if any(row.get("event_id") == request_id for row in persisted.get("approval_events", [])):
            raise ApprovalPending(request_id, exc) from exc
        raise
    synced = _recover_or_pending(master_doc)
    return entry_for(event, synced) | {"projection_status": "complete"}


def _sync_team_db(doc: dict) -> None:
    db_path = TEAM / "team.db"
    if not db_path.exists():
        return
    from trading_team import db
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        db._ensure_column(conn, "approvals", "event_id", "TEXT")
        db._ensure_column(conn, "approvals", "event_hash", "TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS approvals_event_identity "
                     "ON approvals(decision_date,event_id) WHERE event_id IS NOT NULL")
        for event in doc["approval_events"]:
            values = database_event(event)
            row = conn.execute("SELECT decision_date,code,decision_type,decided_at,source,note,plan_hash,event_id,event_hash "
                               "FROM approvals WHERE decision_date=? AND event_id=?", (doc["date"], event["event_id"])).fetchone()
            if row is not None and row != values:
                raise ValueError("approval database event conflicts with primary evidence")
            if row is None:
                conn.execute("INSERT INTO approvals(decision_date,code,decision_type,decided_at,source,note,plan_hash,event_id,event_hash) "
                             "VALUES (?,?,?,?,?,?,?,?,?)", values)
        db.sync_decision(doc, conn, commit=False)


def _recover_or_pending(doc: dict) -> dict:
    events = validate_history(doc)
    _checked_decision(doc["date"], doc["plan_hash"])
    if not events or inspect_projection(doc, TEAM)["status"] == "complete":
        return doc
    try:
        destinations = doc["approval_projection"].get("destinations", {})
        for key, path in (("team_db", TEAM / "team.db"), ("agent_md", TEAM.parent / "AGENT.md")):
            if destinations.get(key) == "synced" and not path.exists():
                raise ValueError(f"approval destination missing: {path.name}; restore before recovery")
        if doc["approval_projection"]["status"] != "pending":
            doc["approval_projection"]["status"] = "pending"
            atomic_write_json(_decision_path(doc["date"]), doc)
        data = load()
        entries = projected_entries(doc)
        keys = {(e["date"], e["plan_hash"], e["code"]) for e in entries}
        data["decisions"] = [row for row in data["decisions"]
                             if (row.get("date"), row.get("plan_hash"), str(row.get("code"))) not in keys] + entries
        save(data)
        _sync_team_db(doc)
        recorded = [log_to_agent_md(entry_for(event, doc)) for event in events]
        if destinations.get("agent_md") == "synced" and not all(recorded):
            raise ValueError("required approval human audit could not be restored")
        doc["approval_projection"].update(status="complete", destinations={
            "team_db": "synced" if (TEAM / "team.db").exists() else "absent",
            "agent_md": "synced" if all(recorded) else "not_configured",
        })
        atomic_write_json(_decision_path(doc["date"]), doc)
    except Exception as exc:
        raise ApprovalPending(events[-1]["event_id"], exc) from exc
    return doc


def status(day: str, *, team: Path | None = None) -> dict:
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("invalid approval date")
    root = team or TEAM
    path = root / "decisions" / day / "decision.json"
    if not path.exists():
        return {"date": day, "status": "missing", "event_count": 0, "events": [], "reasons": []}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        plan = json.loads((root / "outputs" / day / "plan.json").read_text(encoding="utf-8"))
        if doc.get("date") != day or plan.get("date") != day or canonical_plan_hash(plan) != doc.get("plan_hash"):
            raise ValueError("approval plan_hash is not current; re-materialize the plan")
        return {"date": day, "plan_hash": doc["plan_hash"], **inspect_projection(doc, root)}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"date": day, "status": "invalid", "event_count": None, "events": [], "reasons": [str(exc)]}


def recover(day: str) -> dict:
    from pipeline.policy import require
    require("llm_plan")
    with state_lock(TEAM):
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("invalid approval date")
        doc = json.loads(_decision_path(day).read_text(encoding="utf-8"))
        _recover_or_pending(doc)
        return status(day)


def _decision_path(day: str) -> Path:
    return DECISIONS_DIR / day / "decision.json"


def _checked_decision(day: str, plan_hash: str) -> dict:
    path = _decision_path(day)
    if not path.exists():
        raise ValueError(f"主事实源不存在: {path}")
    decision = json.loads(path.read_text(encoding="utf-8"))
    validate_history(decision)
    plan_path = TEAM / "outputs" / day / "plan.json"
    if not plan_path.exists():
        raise ValueError("current plan.json missing; approval refused")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    current_hash = canonical_plan_hash(plan)
    if (plan.get("date") != day or decision.get("date") != day
            or current_hash != plan_hash or decision.get("plan_hash") != current_hash):
        raise ValueError(f"plan_hash 已失效: requested={plan_hash}, current={current_hash}")
    validate_decision_facts(decision, plan)
    return decision


def write_back_to_decision(day: str, code: str, decision: str, plan_hash: str,
                           note: str, decided_at: str,
                           dec: dict | None = None) -> dict:
    """把批复结果回写主事实源 decision.json。

    锁内重读原计划和主事实源；不采用调用方可能过期的 dec 快照。
    """
    with state_lock(TEAM):
        _record(day, code, str(code), decision, plan_hash, note, "direct", None, decided_at)
        doc = json.loads(_decision_path(day).read_text(encoding="utf-8"))
        return next(p for p in doc["plans"] if str(p.get("code")) == str(code))


def _apply_approval(dec: dict, event: dict) -> None:
    code, decision, plan_hash = event["code"], event["decision"], event["plan_hash"]
    market_ok = bool(dec.get("market_ok", False))
    target = None
    for p in dec.get("plans", []):
        if str(p.get("code")) == str(code):
            target = p
            break
    if target is None:
        raise ValueError(f"当前计划不存在标的 {code}")
    target["approval"] = decision
    target["approval_note"] = event["note"]
    target["approved_at"] = event["decided_at"]
    target["approved_plan_hash"] = plan_hash
    target["approval_event_id"] = event["event_id"]
    executable, blocked = compute_executable(target, market_ok, plan_hash)
    target["executable"] = executable
    target["blocked_reason"] = blocked

    # 全部否决单独标记；其余已处理状态沿用原有汇总语义。
    plans = dec.get("plans", [])
    states = [p.get("approval", "pending") for p in plans]
    if plans and all(s == "rejected" for s in states):
        dec["pm_status"] = "rejected"
    elif plans and all(s in VALID for s in states):
        dec["pm_status"] = "approved"
    elif any(s in VALID for s in states):
        dec["pm_status"] = "partial"
    else:
        dec["pm_status"] = "pending"

def log_to_agent_md(entry: dict) -> bool:
    """在 AGENT.md 最新 Loop 条目的「人工决策」追加留痕。"""
    agent_md = TEAM.parent / "AGENT.md"
    if not agent_md.exists():
        return False
    text = agent_md.read_text(encoding="utf-8")
    audit_marker = marker(entry)
    if audit_marker in text:
        return True
    last_loop = text.rfind("\n## Loop #")
    if last_loop == -1:
        return False
    label = {"approved": "批准", "rejected": "否决", "reduced": "降仓批准"}[entry["decision"]]
    line = f"{entry['decided_at'][:10]} 批复 {entry['name']}({entry['code']}): {label}（{entry['source']}）{('；' + entry['note']) if entry['note'] else ''}"
    line += " " + audit_marker
    head, tail = text[:last_loop], text[last_loop:]
    import re
    m = re.search(r"(- \*\*人工决策\*\*: )(.*?)\n", tail)
    if not m:
        return False
    old = m.group(2).strip()
    new = line if old in ("（待填写）", "", "（待填写——含 Loop #5 首日提案：平安 15% 分批买入计划，待批复）") \
        else f"{old}；{line}"
    tail = tail[:m.start()] + m.group(1) + new + "\n" + tail[m.end():]
    atomic_write_text(agent_md, head + tail)
    return True


def decision_for(day: str, code: str, plan_hash: str | None = None) -> dict | None:
    for d in reversed(load()["decisions"]):
        if (d["date"] == day and d["code"] == code
                and (plan_hash is None or d.get("plan_hash") == plan_hash)):
            return d
    return None


def pending(day: str) -> list[dict]:
    """当日提案中尚未批复的（观望/回避类 0% 仓位提案也需要批复确认）。"""
    decision_file = DECISIONS_DIR / day / "decision.json"
    if not decision_file.exists():
        return []
    dec = json.loads(decision_file.read_text(encoding="utf-8"))
    return [p for p in dec.get("plans", []) if p.get("approval", "pending") == "pending"]


def main() -> int:
    import argparse
    global TEAM, FILE, DECISIONS_DIR
    parser = argparse.ArgumentParser(description="Inspect or recover recorded approvals; never submit new intent")
    parser.add_argument("command", choices=("status", "recover"))
    parser.add_argument("date")
    parser.add_argument("--team", type=Path, default=TEAM)
    args = parser.parse_args()
    TEAM = args.team.resolve()
    FILE, DECISIONS_DIR = TEAM / "loops/approvals.json", TEAM / "decisions"
    result = status(args.date) if args.command == "status" else recover(args.date)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in ("complete", "legacy_untracked") else 1


if __name__ == "__main__":
    raise SystemExit(main())
