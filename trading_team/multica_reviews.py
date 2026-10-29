"""Version-bound Multica reviews. Recording a review never authorizes execution."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path
import re
import sqlite3
from uuid import UUID

from trading_team.execution_bars import write_immutable
from trading_team.kimi_daily import TEAM, _sha256, inspect_generation
from trading_team.paper_pipeline import COHORT_FILE, active_paper_cohort
from trading_team.multica_adapter import CHINESE_TASK_TITLES_FROM, MulticaCLI, verify_execution_context
from trading_team.run_ledger import RunLedger
from trading_team.state_lock import state_lock

SCHEMA = "multica_review_bundle.v1"
ROLES = {
    "data-quality": {"name": "Data Quality", "through": 0,
                     "checks": ("completed_session", "source_integrity", "coverage")},
    "market-analysts": {"name": "Market Analysts", "through": 4,
                        "checks": ("facts_supported", "missing_data_disclosed")},
    "strategy-research": {"name": "Strategy Research", "through": 5,
                          "checks": ("counterevidence", "claims_vs_evidence")},
    "paper-trader": {"name": "Paper Trader", "through": 6,
                     "checks": ("intent_constraints", "account_context")},
    "risk-manager": {"name": "Risk Manager", "through": 7,
                     "checks": ("risk_derivation", "portfolio_constraints")},
    "audit-verifier": {"name": "Audit Verifier", "through": 7,
                       "checks": ("version_chain", "review_handoffs")},
}
READ_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error)


class ReviewConflict(ValueError):
    """A remote task already has a different immutable result."""


class ReviewBlocked(ValueError):
    """Current review evidence is not sufficient to enter execution."""


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(value) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _safe(team: Path, relative: str) -> Path:
    path = team / relative
    if not path.resolve().is_relative_to(team.resolve()):
        raise ValueError("review source path escapes workspace")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError("review source symlink forbidden")
        if parent == team:
            break
    return path


def bundle_path(team: Path, day: str, digest: str) -> Path:
    date.fromisoformat(day)
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("review bundle hash invalid")
    return _safe(team, f".runs/multica/reviews/{day}/{digest}.json")


def issue_title(bundle: dict) -> str:
    if bundle["date"] >= CHINESE_TASK_TITLES_FROM:
        labels = {"data-quality": "数据质量", "market-analysts": "市场分析",
                  "strategy-research": "策略研究", "paper-trader": "模拟交易",
                  "risk-manager": "风险管理", "audit-verifier": "审计核验"}
        return f"A股审阅 {labels[bundle['role']]} {bundle['bundle_hash']} {bundle['date']}"
    return f"A-share review {bundle['role']} {bundle['bundle_hash']} {bundle['date']}"


def _source(team: Path, day: str, ledger: RunLedger) -> dict:
    date.fromisoformat(day)
    evidence = inspect_generation(team, day, ledger)
    publisher = evidence["publisher"]
    if publisher["status"] != "succeeded" or publisher["output_hash"] != evidence["receipt_hash"]:
        raise ValueError("review source publication is not acknowledged")
    generation_path = _safe(team, f"outputs/{day}/generation.json")
    generation = json.loads(generation_path.read_bytes())
    state = json.loads(_safe(team, ".runs/multica/bootstrap.json").read_bytes())
    try:
        names = ["Research Lead", *[r["name"] for r in ROLES.values()]]
        agents = {name: str(UUID(state["agent_ids"][name])) for name in names}
        binding = {"project_id": str(UUID(state["project_id"])),
                   "squad_id": str(UUID(state["squad_id"])), "agents": agents}
        if len(set(agents.values())) != len(names):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("review role binding is incomplete or not independent") from None
    producer_run = ledger.get_run(generation["run_id"])
    attempts = [item for item in producer_run["attempts"] if item["attempt"] == publisher["workflow_attempt"]]
    if producer_run["workflow"] != "daily_research" or producer_run["business_date"] != day or len(attempts) != 1:
        raise ValueError("review producer attempt missing")
    producer = attempts[0]["external_context"]
    if producer:
        if (producer.get("provider") != "multica" or producer.get("verification") != "remote_verified"
                or producer.get("role") != "Research Lead" or producer.get("agent_id") != agents["Research Lead"]
                or producer.get("project_id") != binding["project_id"]):
            raise ValueError("review producer is not the independent publishing lead")
        binding["workspace_id"] = str(UUID(producer["workspace_id"]))
        producer = {key: producer[key] for key in
                    ("provider", "verification", "role", "agent_id", "task_id", "project_id", "workspace_id")}
    expected_hashes = {**generation["inputs"],
                       **{f"outputs/{day}/{name}": digest for name, digest in generation["outputs"].items()},
                       f"outputs/{day}/generation.json": evidence["receipt_hash"]}
    paths = set(expected_hashes)
    paths.update((f"outputs/{day}/plan.json", f"outputs/{day}/generation.json"))
    archive = json.loads(_safe(team, f"context/{day}/execution_archive.json").read_bytes())
    paths.add(f"market_archive/{archive['date']}/{archive['payload_hash']}.json")
    files = {}
    for relative in sorted(paths):
        if relative == COHORT_FILE:
            if active_paper_cohort(team) is None:
                raise ValueError("review Paper cohort source missing")
            path = team.parent / COHORT_FILE
        else:
            path = _safe(team, relative)
        content = path.read_text(encoding="utf-8")
        if relative in expected_hashes and _sha256(content) != expected_hashes[relative]:
            raise ValueError("review source content differs from verified generation")
        files[relative] = {"sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "text": content}
    # Detect non-cooperating writers while collecting the inline, bounded input package.
    if inspect_generation(team, day, ledger)["receipt_hash"] != evidence["receipt_hash"]:
        raise ValueError("review source changed during preparation")
    return {"source": {"run_id": generation["run_id"], "generation_hash": evidence["receipt_hash"],
                       "producer": producer,
                       "files_hash": _hash({name: item["sha256"] for name, item in files.items()}),
                       "plan_hash": evidence["plan_hash"], "data_asof": evidence["data_asof"]},
            "binding": binding, "files": files}


def _bundle(day: str, role: str, source: dict, upstream: dict) -> dict:
    definition = ROLES[role]
    files = {}
    for name, item in source["files"].items():
        if name.startswith(f"outputs/{day}/"):
            basename = Path(name).name
            number = re.match(r"0([1-7])_", basename)
            if number and int(number[1]) > definition["through"]:
                continue
            if basename in ("plan.json", "daily_conclusion.md") and definition["through"] < 6:
                continue
        files[name] = item
    value = {"schema": SCHEMA, "date": day, "role": role, "source": source["source"],
             "binding": source["binding"], "files": files, "upstream": dict(upstream),
             "checks": list(definition["checks"]), "execution_authorized": False}
    return {**value, "bundle_hash": _hash(value)}


def _response(document: dict, bundle: dict) -> dict:
    if (not isinstance(document, dict)
            or set(document) != {"schema", "bundle_hash", "role", "verdict", "summary", "checks"}
            or document["schema"] != "multica_review_response.v1"
            or document["bundle_hash"] != bundle["bundle_hash"] or document["role"] != bundle["role"]
            or document["verdict"] not in ("accept", "reject")
            or not isinstance(document["summary"], str) or not 0 < len(document["summary"].strip()) <= 4000
            or not isinstance(document["checks"], list)):
        raise ValueError("review response contract invalid")
    names, results = [], []
    for check in document["checks"]:
        if (not isinstance(check, dict) or set(check) != {"name", "result", "reason", "evidence"}
                or check["name"] not in bundle["checks"] or check["result"] not in ("pass", "fail", "unknown")
                or not isinstance(check["reason"], str) or not 0 < len(check["reason"].strip()) <= 2000
                or not isinstance(check["evidence"], list) or not 0 < len(check["evidence"]) <= 20
                or any(not isinstance(ref, str) or ref not in bundle["files"] for ref in check["evidence"])):
            raise ValueError("review check or evidence invalid")
        names.append(check["name"])
        results.append(check["result"])
    if (len(names) != len(set(names)) or set(names) != set(bundle["checks"])
            or (document["verdict"] == "accept") != all(result == "pass" for result in results)):
        raise ValueError("review coverage or verdict inconsistent")
    return json.loads(_json(document))


def _workflow(role: str) -> str:
    return f"multica_review.{role}"


def _recorded(run: dict, bundle: dict) -> dict:
    if not run["attempts"]:
        raise ValueError("review attempt acknowledgement missing")
    receipt = run["metadata"]["receipt"]
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    context = receipt["external_context"]
    response = _response(receipt["response"], bundle)
    role = ROLES[bundle["role"]]["name"]
    for key in ("agent_id", "task_id", "workspace_id", "project_id", "issue_id"):
        UUID(context[key])
    unsigned_context = {key: value for key, value in context.items() if key != "evidence_hash"}
    if context["evidence_hash"] != hashlib.sha256(json.dumps(unsigned_context, sort_keys=True).encode("utf-8")).hexdigest():
        raise ValueError("review remote evidence changed")
    if (receipt["receipt_hash"] != _hash(unsigned) or receipt["schema"] != "multica_review_receipt.v1"
            or receipt["bundle_hash"] != bundle["bundle_hash"] or receipt["run_id"] != run["run_id"]
            or receipt["execution_authorized"] is not False or context["verification"] != "remote_verified"
            or context["provider"] != "multica" or context["observed_status"] != "running"
            or context["role"] != role or context["agent_id"] != bundle["binding"]["agents"][role]
            or context["project_id"] != bundle["binding"]["project_id"]
            or bundle["binding"].get("workspace_id", context["workspace_id"]) != context["workspace_id"]
            or any(r["external_context"]["workspace_id"] != context["workspace_id"] for r in bundle["upstream"].values())
            or context["issue_title"] != issue_title(bundle)
            or context["task_id"] == bundle["source"]["producer"].get("task_id")
            or run["attempts"][-1]["external_context"] != context or run["attempts"][-1]["status"] != "succeeded"
            or run["metadata"]["response"] != response):
        raise ValueError("review receipt or remote binding invalid")
    steps = run["steps"]
    if (not steps or steps[-1]["step_name"] != "record_review" or steps[-1]["status"] != "succeeded"
            or steps[-1]["input_hash"] != bundle["bundle_hash"] or steps[-1]["role"] != role
            or steps[-1]["workflow_attempt"] != run["attempt"] or steps[-1]["output_hash"] != receipt["receipt_hash"]):
        raise ValueError("review ledger acknowledgement missing")
    return receipt


def _walk(team: Path, day: str, ledger: RunLedger, source: dict, until: str | None = None):
    upstream, states = {}, {}
    blocked = False
    for role in ROLES:
        if role == until:
            if blocked:
                raise ValueError("review upstream is missing, rejected, stale or failed")
            return _bundle(day, role, source, upstream)
        if blocked:
            states[role] = {"status": "blocked"}
            continue
        bundle = _bundle(day, role, source, upstream)
        latest = ledger.latest_run(_workflow(role), day)
        if not latest:
            state = {"status": "pending", "bundle_hash": bundle["bundle_hash"]}
        elif latest["status"] != "succeeded":
            state = {"status": "incomplete", "run_id": latest["run_id"]}
        elif latest["metadata"].get("bundle_hash") != bundle["bundle_hash"]:
            state = {"status": "stale", "run_id": latest["run_id"]}
        else:
            try:
                sealed = json.loads(bundle_path(team, day, bundle["bundle_hash"]).read_bytes())
                if sealed != bundle:
                    raise ValueError("review bundle changed")
                receipt = _recorded(ledger.get_run(latest["run_id"]), bundle)
                state = {"status": "accepted" if receipt["response"]["verdict"] == "accept" else "rejected",
                         "run_id": latest["run_id"], "receipt_hash": receipt["receipt_hash"]}
                if state["status"] == "accepted":
                    upstream[role] = receipt
            except READ_ERRORS:
                state = {"status": "invalid", "run_id": latest["run_id"]}
        states[role] = state
        blocked = state["status"] != "accepted"
    return states


def _recorded_history(team: Path, day: str, ledger: RunLedger) -> dict:
    history = {}
    for role in ROLES:
        latest = ledger.latest_run(_workflow(role), day)
        if not latest:
            continue
        run_id = latest["run_id"]
        if latest["status"] != "succeeded":
            history[role] = {"status": "incomplete", "run_id": run_id}
            continue
        try:
            digest = latest["metadata"]["bundle_hash"]
            sealed = json.loads(bundle_path(team, day, digest).read_bytes())
            unsigned = {key: value for key, value in sealed.items() if key != "bundle_hash"}
            if (sealed["schema"] != SCHEMA or sealed["date"] != day or sealed["role"] != role
                    or sealed["bundle_hash"] != digest or _hash(unsigned) != digest
                    or sealed["execution_authorized"] is not False
                    or latest["plan_hash"] != sealed["source"]["plan_hash"]):
                raise ValueError("historical review bundle invalid")
            receipt = _recorded(ledger.get_run(run_id), sealed)
            history[role] = {"status": "accepted" if receipt["response"]["verdict"] == "accept" else "rejected",
                             "run_id": run_id, "receipt_hash": receipt["receipt_hash"], "bundle_hash": digest}
        except READ_ERRORS:
            history[role] = {"status": "invalid", "run_id": run_id}
    return history


def prepare_review(day: str, role: str, *, team: Path = TEAM) -> dict:
    if role not in ROLES:
        raise ValueError("unknown review role")
    with state_lock(team):
        ledger = RunLedger(team / ".runs/ledger.db", readonly=True)
        bundle = _walk(team, day, ledger, _source(team, day, ledger), until=role)
        write_immutable(bundle_path(team, day, bundle["bundle_hash"]), _json(bundle))
        return bundle


def review_status(day: str, *, team: Path = TEAM) -> dict:
    result = {"schema": "multica_review_status.v1", "date": day,
              "execution_authorized": False, "production_complete": False, "roles": {}}
    ledger = None
    try:
        ledger = RunLedger(team / ".runs/ledger.db", readonly=True)
        source = _source(team, day, ledger)
        states = _walk(team, day, ledger, source)
        return {**result, "source": source["source"], "roles": states,
                "status": "accepted" if all(s["status"] == "accepted" for s in states.values()) else "incomplete"}
    except READ_ERRORS as exc:
        history = {}
        if ledger is not None:
            try:
                history = _recorded_history(team, day, ledger)
            except READ_ERRORS:
                pass
        return {**result, "status": "source_unverified", "recorded": history,
                "error": f"Review source unavailable ({type(exc).__name__})"}


def require_reviews(day: str, plan_hash: str, *, team: Path = TEAM) -> dict:
    """Read-only prerequisite; callers hold the shared state lock through their writes."""
    status = review_status(day, team=team)
    if status["status"] != "accepted":
        blocked = next((f"{role}:{item['status']}" for role, item in status["roles"].items()
                        if item["status"] != "accepted"), status["status"])
        raise ReviewBlocked(f"Multica review gate blocked: {blocked}")
    source = status["source"]
    if source["plan_hash"] != plan_hash:
        raise ReviewBlocked("Multica review gate blocked: plan version mismatch")
    proof = {"schema": "multica_review_gate.v1", "date": day, "plan_hash": plan_hash,
             "generation_hash": source["generation_hash"], "files_hash": source["files_hash"],
             "receipts": {role: item["receipt_hash"] for role, item in status["roles"].items()}}
    return {**proof, "gate_hash": _hash(proof)}


def submit_review(day: str, role: str, digest: str, document: dict, issue_id: str, *,
                  team: Path = TEAM, cli: MulticaCLI | None = None, environ: dict | None = None) -> dict:
    if role not in ROLES:
        raise ValueError("unknown review role")
    path = bundle_path(team, day, digest)
    context, run_id, step_id, metadata = {}, None, None, {"bundle_hash": digest, "role": role}
    ledger = RunLedger(team / ".runs/ledger.db", readonly=True)
    try:
        sealed = json.loads(path.read_bytes())
        expected = _walk(team, day, ledger, _source(team, day, ledger), until=role)
        if sealed != expected or expected["bundle_hash"] != digest:
            raise ValueError("review bundle or source changed")
        checked = _response(document, sealed)
        context = verify_execution_context(day, issue_id, cli=cli, environ=environ,
            state_path=team / ".runs/multica/bootstrap.json",
            title_prefix=issue_title(sealed).removesuffix(f" {day}"))
        if (context["role"] != ROLES[role]["name"]
                or context["agent_id"] != sealed["binding"]["agents"][ROLES[role]["name"]]
                or context["project_id"] != sealed["binding"]["project_id"]
                or context["task_id"] == sealed["source"]["producer"].get("task_id")
                or sealed["binding"].get("workspace_id", context["workspace_id"]) != context["workspace_id"]
                or any(r["external_context"]["workspace_id"] != context["workspace_id"] for r in sealed["upstream"].values())):
            raise ValueError("Multica review role/binding mismatch")
        with state_lock(team):
            ledger = RunLedger(team / ".runs/ledger.db")
            current = _walk(team, day, ledger, _source(team, day, ledger), until=role)
            if current != sealed or json.loads(bundle_path(team, day, digest).read_bytes()) != sealed:
                raise ValueError("review bundle or upstream changed during verification")
            request_hash = _hash({"bundle_hash": digest, "task_id": context["task_id"]})
            with ledger._connect() as conn:
                row = conn.execute("SELECT run_id FROM workflow_runs WHERE workflow=? AND business_date=? "
                    "AND input_hash=? ORDER BY started_at DESC,rowid DESC LIMIT 1",
                    (_workflow(role), day, request_hash)).fetchone()
            prior = ledger.get_run(row["run_id"]) if row else None
            if prior:
                if prior["metadata"].get("response") != checked:
                    raise ReviewConflict("review task result conflict")
                if prior["status"] == "succeeded":
                    return {"receipt": _recorded(prior, sealed), "reused": True, "execution_authorized": False,
                            "is_latest": ledger.latest_run(_workflow(role), day)["run_id"] == prior["run_id"]}
            metadata["response"] = checked
            run_id = ledger.start_run(_workflow(role), day, input_hash=request_hash,
                trigger_source=f"multica:{context['issue_id']}:{context['task_id']}",
                external_context=context, metadata=metadata)
            step_id = ledger.start_step(run_id, "record_review", role=context["role"], input_hash=digest)
            receipt = {"schema": "multica_review_receipt.v1", "run_id": run_id, "bundle_hash": digest,
                       "external_context": context, "response": checked, "execution_authorized": False}
            receipt["receipt_hash"] = _hash(receipt)
            metadata["receipt"] = receipt
            ledger.finish_step(step_id, output_hash=receipt["receipt_hash"], metadata={"receipt": receipt})
            ledger.finish_run(run_id, status="succeeded", plan_hash=sealed["source"]["plan_hash"], metadata=metadata)
            return {"receipt": receipt, "reused": False, "is_latest": True, "execution_authorized": False}
    except Exception as exc:
        # Never change a completed task's immutable result to record a conflicting retry.
        if isinstance(exc, ReviewConflict):
            raise
        with state_lock(team):
            ledger = RunLedger(team / ".runs/ledger.db")
            if run_id is None:
                run_id = ledger.start_run(_workflow(role), day, trigger_source="multica_review_rejected",
                    input_hash=digest, external_context=context, metadata=metadata, resume=False)
                step_id = ledger.start_step(run_id, "verify_review", role=ROLES[role]["name"])
            error = f"Review not recorded ({type(exc).__name__})"
            ledger.finish_step(step_id, status="failed", error=error)
            ledger.finish_run(run_id, status="failed", error=error, metadata=metadata)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "status", "submit"):
        command = sub.add_parser(name)
        command.add_argument("--date", required=True)
        command.add_argument("--team", type=Path, default=TEAM)
        if name != "status":
            command.add_argument("--role", choices=ROLES, required=True)
        if name == "submit":
            command.add_argument("--bundle-hash", required=True)
            command.add_argument("--response", type=Path, required=True)
            command.add_argument("--issue-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            bundle = prepare_review(args.date, args.role, team=args.team)
            result = {"bundle_hash": bundle["bundle_hash"], "issue_title": issue_title(bundle),
                      "path": str(bundle_path(args.team, args.date, bundle["bundle_hash"])),
                      "assignee_id": bundle["binding"]["agents"][ROLES[args.role]["name"]]}
        elif args.command == "status":
            result = review_status(args.date, team=args.team)
        else:
            result = submit_review(args.date, args.role, args.bundle_hash, json.loads(args.response.read_bytes()),
                                   args.issue_id, team=args.team)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "status" and result["status"] != "accepted":
            raise SystemExit(2)
    except READ_ERRORS + (RuntimeError,) as exc:
        print(json.dumps({"status": "failed", "error": f"Review operation failed ({type(exc).__name__})"}))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
