# -*- coding: utf-8 -*-
"""Use Moonshot API only when the primary Kimi automation exhausts quota."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trading_team.kimi_daily import DailyMeetingRunner, KimiClient, inspect_generation, load_local_env
from trading_team.run_ledger import RunLedger

HOME = Path.home()
AUTOMATION_ID = "automation_7a27faee-d643-4f35-a89b-daa3798b2452"
AUTOMATION_DIR = (
    HOME / "Library/Application Support/kimi-desktop/daimon-share/daimon/agents/main/"
    "blueprint/automations" / AUTOMATION_ID
)
LOG_DIR = HOME / "Library/Logs/kimi-desktop"
TEAM = Path(__file__).resolve().parent
QUOTA_MARKERS = ("resource_exhausted", "usage limit", "insufficient balance")


def latest_scheduled_run(day: str, automation_dir: Path = AUTOMATION_DIR) -> dict | None:
    target = date.fromisoformat(day)
    matches = []
    for path in (automation_dir / "runs").glob("*/run.json"):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))["run"]
            started = datetime.fromisoformat(run["startedAt"].replace("Z", "+00:00"))
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            continue
        local_day = started.astimezone(ZoneInfo("Asia/Shanghai")).date()
        logs = run.get("logs", [])
        if local_day == target and any("triggered by schedule" in line for line in logs):
            matches.append(run)
    return max(matches, key=lambda item: item["startedAt"], default=None)


def quota_exhausted(run_id: str, log_dir: Path = LOG_DIR) -> bool:
    for path in log_dir.glob("main*.log"):
        try:
            with path.open(encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    lower = line.lower()
                    if run_id in line and any(marker in lower for marker in QUOTA_MARKERS):
                        return True
        except OSError:
            continue
    return False


def fallback(day: str, *, check_only: bool = False) -> dict:
    date.fromisoformat(day)
    run = latest_scheduled_run(day)
    if run and run.get("status") == "running":
        return {"date": day, "status": "skipped", "reason": "Kimi 仍在运行"}

    output = TEAM / "outputs" / day
    if output.exists() and any(output.iterdir()):
        try:
            if not (output / "generation.json").is_file():
                raise ValueError("generation evidence missing; legacy or partial report requires explicit recovery")
            ledger = RunLedger(TEAM / ".runs/ledger.db", readonly=True)
            evidence = inspect_generation(TEAM, day, ledger)
            publisher = evidence["publisher"]
            if (publisher["status"] != "succeeded"
                    or publisher["output_hash"] != evidence["receipt_hash"]):
                raise ValueError("generation publication acknowledgement requires recovery")
            latest = ledger.latest_run("daily_research", day)
            if (not latest or latest["status"] != "succeeded"
                    or latest["plan_hash"] != evidence["plan_hash"]):
                raise ValueError("latest daily_research workflow is incomplete or version-mismatched")
        except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as exc:
            return {"date": day, "status": "blocked", "code": "existing_generation_unverified",
                    "reason": "已有日报未通过生成/完成证据校验；保留原件，须显式恢复，不自动调用备用 API",
                    "error": str(exc)}
        return {"date": day, "status": "skipped", "code": "verified_generation",
                "reason": "日报生成凭据和最近研究运行已校验；不重跑、不重发",
                "plan_hash": evidence["plan_hash"], "run_id": latest["run_id"]}

    if run is None:
        return {"date": day, "status": "skipped", "reason": "未发现 Kimi 当日定时运行"}
    if run.get("status") != "failed":
        return {
            "date": day,
            "status": "blocked",
            "code": "primary_completed_without_generation",
            "reason": f"Kimi 状态为 {run.get('status')}，但无已验证日报；不触发 API 兜底",
        }
    if not quota_exhausted(run["runId"]):
        return {"date": day, "status": "blocked", "code": "primary_failure_not_quota",
                "reason": "Kimi 失败但不是额度耗尽，不触发 API 兜底"}
    if check_only:
        return {"date": day, "status": "fallback_required", "primary_run_id": run["runId"],
                "reason": "已确认主任务额度耗尽；仅检查，未调用备用 API"}

    load_local_env()
    import os
    client = KimiClient(
        os.environ.get("MOONSHOT_API_KEY", ""),
        os.environ.get("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1"),
        os.environ.get("MOONSHOT_MODEL", "kimi-k3"),
    )
    return DailyMeetingRunner(client, team=TEAM).run(day, trigger_source=f"kimi_quota_fallback:{run['runId']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
    parser.add_argument("--check", action="store_true", help="Read-only evidence and fallback eligibility check")
    args = parser.parse_args()
    result = fallback(args.date, check_only=args.check)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit({"blocked": 1, "fallback_required": 2}.get(result["status"], 0))


if __name__ == "__main__":
    main()
