# -*- coding: utf-8 -*-
"""同日多源一致性校验：decision.json（主事实源）/ approvals.json（日志）/ plan.json（草稿）/ team.db

改进计划 Phase 0 核心门禁：
- 审批必须已回写主事实源（decision.json 的 per-plan approval），只写 approvals.json 视为冲突；
- market_ok=false 时任何买入计划不得处于可执行状态；
- team.db 的 pm_status 必须与 decision.json 一致；
- plan.json 仅为草稿：与 decision.json 方向冲突记为冲突（因为它会误导审批人），
  字符串仓位等 schema 问题记为警告。

用法：
    python trading_team/consistency_check.py            # 校验最新日期
    python trading_team/consistency_check.py 2026-08-22 # 校验指定日期
退出码：0 = 通过（可有警告）；1 = 存在冲突（fail closed）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

TEAM = Path(__file__).resolve().parent
ROOT = TEAM.parent
DECISIONS_DIR = TEAM / "decisions"
OUTPUTS_DIR = TEAM / "outputs"
APPROVALS_FILE = TEAM / "loops" / "approvals.json"
TEAM_DB = TEAM / "team.db"

sys.path.insert(0, str(TEAM))
from schemas import is_buy, parse_pct, validate_plan, validate_portfolio, SchemaError  # noqa: E402


def _load_json(path: Path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def check_day(day: str) -> dict:
    """返回 {"ok": bool, "conflicts": [...], "warnings": [...]}"""
    conflicts, warnings = [], []

    dec = _load_json(DECISIONS_DIR / day / "decision.json")
    if dec is None:
        return {"ok": False, "conflicts": [f"主事实源缺失: decisions/{day}/decision.json"],
                "warnings": warnings}
    plans = dec.get("plans", [])
    market_ok = bool(dec.get("market_ok", False))
    pm_status = dec.get("pm_status", "pending")
    plan_by_code = {str(p.get("code")): p for p in plans}

    # ── 1. 主事实源自身：schema + market_ok 约束 ─────────────────
    for p in plans:
        for prob in validate_plan(p):
            warnings.append(f"decision.json {p.get('code')}: {prob}")
        approval = p.get("approval", "pending")
        executable = bool(p.get("executable", False))
        try:
            pct = parse_pct(p.get("position_pct"))
        except SchemaError:
            pct = 0.0
            if executable:
                conflicts.append(f"{p.get('code')}: position_pct 无法解析却被标 executable")
        if executable and approval not in ("approved", "reduced"):
            conflicts.append(
                f"{p.get('code')}: executable=true 但 approval={approval}（审批未回写主事实源）"
            )
        if executable and is_buy(str(p.get("direction", "")), pct) and pct > 0 and not market_ok:
            conflicts.append(f"{p.get('code')}: market_ok=false 但买入计划被标记 executable")

    # ── 1b. 组合级确定性风控（LLM 不得绕过）─────────────────────
    for prob in validate_portfolio(plans, market_ok):
        conflicts.append(f"组合风控: {prob}")

    # ── 2. approvals.json（日志）必须与主事实源同步 ──────────────
    appr = _load_json(APPROVALS_FILE) or {"decisions": []}
    seen = {}
    for d in appr.get("decisions", []):
        if d.get("date") == day:
            seen[str(d.get("code"))] = d  # record() 保证同日同码唯一
    for code, a in seen.items():
        p = plan_by_code.get(code)
        if p is None:
            warnings.append(f"approvals.json 含 {code} 的批复，但 decision.json 无此计划")
            continue
        master_approval = p.get("approval", "pending")
        if master_approval != a.get("decision"):
            conflicts.append(
                f"{code}: approvals.json={a.get('decision')} 但 decision.json approval={master_approval}"
                f"（审批未回写主事实源）"
            )

    # ── 3. plan.json（草稿）只读比对 ────────────────────────────
    plan_doc = _load_json(OUTPUTS_DIR / day / "plan.json")
    if plan_doc is not None:
        for prop in plan_doc.get("proposals", []):
            code = str(prop.get("code"))
            p = plan_by_code.get(code)
            for prob in validate_plan(prop):
                warnings.append(f"plan.json(草稿) {code}: {prob}")
            if p is None:
                warnings.append(f"plan.json(草稿) 含 {code}，decision.json 无此计划")
                continue
            draft_dir, master_dir = str(prop.get("direction", "")), str(p.get("direction", ""))
            try:
                draft_pct = parse_pct(prop.get("position_pct"))
                draft_buy = is_buy(draft_dir, draft_pct) and draft_pct > 0
            except SchemaError:
                # 仓位解析失败时退化为仅按方向判断买卖性质
                draft_pct = "unparsed"
                draft_buy = is_buy(draft_dir, 1.0)
            try:
                master_pct = parse_pct(p.get("position_pct"))
                master_buy = is_buy(master_dir, master_pct) and master_pct > 0
            except SchemaError:
                master_pct = "unparsed"
                master_buy = is_buy(master_dir, 1.0)
            if draft_buy != master_buy:
                conflicts.append(
                    f"{code}: plan.json 草稿方向={draft_dir}({draft_pct}) 与 "
                    f"decision.json={master_dir}({master_pct}) 买卖性质冲突"
                )

    # ── 4. team.db 与主事实源一致 ───────────────────────────────
    if TEAM_DB.exists():
        conn = sqlite3.connect(TEAM_DB)
        row = conn.execute("SELECT pm_status FROM decisions WHERE date=?", (day,)).fetchone()
        conn.close()
        if row is None:
            warnings.append(f"team.db 无 {day} 决策记录（需重跑 db.py migrate）")
        elif row[0] != pm_status:
            conflicts.append(f"team.db pm_status={row[0]} 与 decision.json={pm_status} 不一致")

    return {"ok": not conflicts, "conflicts": conflicts, "warnings": warnings}


def main() -> int:
    if len(sys.argv) > 1:
        days = [sys.argv[1]]
    else:
        days = sorted([d.name for d in DECISIONS_DIR.iterdir() if d.is_dir()], reverse=True)[:1]
    if not days:
        print("无决策目录")
        return 1
    exit_code = 0
    for day in days:
        r = check_day(day)
        icon = "✅" if r["ok"] else "❌"
        print(f"{icon} {day}: conflicts={len(r['conflicts'])}, warnings={len(r['warnings'])}")
        for c in r["conflicts"]:
            print(f"   [冲突] {c}")
        for w in r["warnings"]:
            print(f"   [警告] {w}")
        if not r["ok"]:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
