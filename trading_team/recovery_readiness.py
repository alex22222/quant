"""Read-only restoration review; never activates schedules, accounts or remote runs."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

from trading_team.approvals import status as approval_status
from trading_team.collect import DEFAULT_TRADING_CALENDAR
from trading_team.execution_bars import SHANGHAI, TEAM
from trading_team.notification_outbox import summary as notification_summary
from trading_team.operational_snapshot import operational_snapshot
from trading_team.paper_reconcile import reconcile_accounts
from trading_team.workflow_recovery import _runtime, verify_workflow_backup

READ_ERRORS = (OSError, ValueError, TypeError, KeyError, sqlite3.Error)


def review(*, restored: Path, manifest_hash: str, runtime_root: Path = TEAM.parent,
           calendar_path: Path = DEFAULT_TRADING_CALENDAR, now: datetime | None = None) -> dict:
    now = now or datetime.now(SHANGHAI)
    result = {"schema_version": "recovery_readiness.v1", "status": "blocked", "reviewed_at": now.isoformat(),
              "restored_root": str(restored), "manifest_hash": manifest_hash,
              "snapshot": {"verified": False}, "blockers": [], "execution_authorized": False,
              "external_gap_reconciled": False, "dependencies_verified": False,
              "manual_requirements": [
                  "Reconcile post-snapshot orders, approvals and provider message receipts against external records",
                  "Install and verify the pinned runtime, dependencies, calendar and credentials in isolation",
                  "Review current data and positions, retain unresolved historical conflicts and evidence gaps",
                  "Obtain explicit activation authorization and ensure only one active writer and scheduler",
                  "Independently verify offsite recovery; a local snapshot is not offsite disaster recovery",
              ]}
    blockers = result["blockers"]
    try:
        manifest = verify_workflow_backup(restored, manifest_hash)
        receipt = json.loads((restored / "restore.json").read_text(encoding="utf-8"))
        if (receipt.get("status") != "verified_isolated" or receipt.get("manifest_hash") != manifest_hash
                or receipt.get("path") != str(restored.resolve()) or receipt.get("execution_authorized") is not False
                or receipt.get("activation") != "manual_review_required_no_runtime_or_scheduler_installed"):
            raise ValueError("isolated restore receipt invalid")
        if restored.resolve() == Path(manifest["source_root"]).resolve() or restored.resolve() == TEAM.parent.resolve():
            raise ValueError("restoration review must target an isolated directory")
        result["snapshot"] = {"verified": True, "source_root": manifest["source_root"]}
    except (AttributeError, *READ_ERRORS) as exc:
        blockers.append("restored integrity: " + str(exc))
        return result
    try:
        expected = manifest.get("runtime_fingerprints", {})
        actual = _runtime(runtime_root)
        changed = sorted(key for key in expected.keys() & actual.keys() if expected[key] != actual[key])
        missing = sorted(expected.keys() - actual.keys())
        added = sorted(actual.keys() - expected.keys())
        complete = {"console/serve.py", "console/index.html", "trading_team/kimi_daily.py"} <= expected.keys()
        matched = complete and not (changed or missing or added)
        result["runtime"] = {"status": "matched" if matched else "mismatch", "root": str(runtime_root),
                             "changed": changed, "missing": missing, "added": added,
                             "dependency_manifest_recorded": "requirements.txt" in expected,
                             "fingerprint_scope": "recorded_files_only_not_dependencies"}
        if not matched:
            blockers.append("runtime fingerprint mismatch or incomplete baseline")
    except READ_ERRORS as exc:
        result["runtime"] = {"status": "unavailable", "error": str(exc)}
        blockers.append("runtime cannot be verified")
    try:
        recorded = {name: item["hash"] for name, item in manifest["files"].items()
                    if name.startswith("trading_team/prompts/") and name.endswith(".md")}
        prompt_dir = runtime_root / "trading_team/prompts"
        if prompt_dir.is_symlink():
            raise ValueError("research prompt directory is a symlink")
        actual = {}
        for path in prompt_dir.rglob("*"):
            if path.is_symlink():
                raise ValueError("research prompt path is a symlink")
            if path.is_file() and path.suffix == ".md":
                actual[path.relative_to(runtime_root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        changed = sorted(key for key in recorded.keys() & actual.keys() if recorded[key] != actual[key])
        missing = sorted(recorded.keys() - actual.keys())
        added = sorted(actual.keys() - recorded.keys())
        result["prompts"] = {"status": "mismatch" if changed or missing or added else "matched",
                             "changed": changed, "missing": missing, "added": added}
        if result["prompts"]["status"] != "matched":
            blockers.append("research prompt mismatch")
    except READ_ERRORS as exc:
        result["prompts"] = {"status": "unavailable", "error": str(exc)}
        blockers.append("research prompts cannot be verified")
    try:
        path = runtime_root / "docs/MULTICA_PRODUCTION_RUNBOOK.md"
        if path.is_symlink():
            raise ValueError("production runbook is a symlink")
        recorded = manifest["files"].get("docs/MULTICA_PRODUCTION_RUNBOOK.md", {}).get("hash")
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        matched = recorded is not None and current == recorded
        result["runbook"] = {"status": "matched" if matched else "mismatch"}
        if not matched:
            blockers.append("production runbook mismatch")
    except READ_ERRORS as exc:
        result["runbook"] = {"status": "unavailable", "error": str(exc)}
        blockers.append("production runbook cannot be verified")
    team = restored / "trading_team"
    try:
        snapshot = operational_snapshot(team=team, calendar_path=calendar_path, now=now)
        result["operational"] = snapshot
        if snapshot["ledger_status"] != "available":
            blockers.append("workflow ledger unavailable")
        if snapshot["data"]["status"] != "available" or snapshot["data"].get("freshness") != "current":
            blockers.append("research data invalid, stale or unknown")
        research = snapshot["research"]
        if research["status"] != "available" or research["generation"]["status"] != "verified":
            blockers.append("research publication evidence incomplete")
        if research["risk_review"]["status"] != "validated_contract":
            blockers.append("risk contract unavailable or invalid")
        audit = reconcile_accounts(team=team, calendar_path=calendar_path, now=now)
        result["accounts"] = audit
        for name, item in audit["portfolios"].items():
            if item["status"] != "checked":
                blockers.append(f"{name} account: {item['status']}")
        notifications = notification_summary(team=team)
        result["notifications"] = notifications
        if notifications.get("status") in ("invalid", "attention") or any(
                notifications.get("counts", {}).get(key) for key in ("unknown", "invalid")):
            blockers.append("notification delivery or record integrity unresolved")
        result["approvals"] = []
        for path in sorted((team / "decisions").glob("*/decision.json")):
            item = approval_status(path.parent.name, team=team)
            result["approvals"].append({"date": path.parent.name, "status": item["status"]})
            if item["status"] in ("pending", "invalid"):
                blockers.append(f"approval {path.parent.name}: {item['status']}")
    except (AttributeError, *READ_ERRORS) as exc:
        blockers.append("business verification failed: " + str(exc))
    result["status"] = "blocked" if blockers else "manual_review_required"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restored", type=Path, required=True)
    parser.add_argument("--manifest-hash", required=True)
    parser.add_argument("--runtime-root", type=Path, default=TEAM.parent)
    parser.add_argument("--calendar", type=Path, default=DEFAULT_TRADING_CALENDAR)
    args = parser.parse_args()
    result = review(restored=args.restored, manifest_hash=args.manifest_hash,
                    runtime_root=args.runtime_root, calendar_path=args.calendar)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(1 if result["status"] == "blocked" else 0)


if __name__ == "__main__":
    main()
