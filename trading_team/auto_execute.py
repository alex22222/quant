#!/usr/bin/env python3
"""
trading_team/auto_execute.py —— 自动执行 approved 但未执行的决策

Blueprint Code Automation 入口: run(ctx)
本地测试: python trading_team/auto_execute.py [--dry-run]
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
TEAM_DB = ROOT / "trading_team" / "team.db"


def get_pending_decisions():
    """获取所有 approved 但未执行的决策日期"""
    if not TEAM_DB.exists():
        return []
    conn = sqlite3.connect(TEAM_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT date FROM decisions
        WHERE pm_status = 'approved' AND executed_at IS NULL
        ORDER BY date ASC
    """).fetchall()
    conn.close()
    return [r["date"] for r in rows]


def _execute_pending(dry_run=False):
    """核心逻辑：执行所有 pending 决策"""
    pending = get_pending_decisions()
    if not pending:
        return {
            "message": "没有待执行的 approved 决策",
            "pending_count": 0,
            "executed": [],
            "ok": 0, "skip": 0, "fail": 0
        }

    print(f"[auto_execute] 发现 {len(pending)} 个待执行决策: {pending}")

    sys.path.insert(0, str(ROOT / "trading_team"))
    from execute import execute  # noqa: E402

    ok_count = skip_count = fail_count = 0
    executed_days = []

    for day in pending:
        print(f"\n[auto_execute] 开始执行 {day} ...")
        if dry_run:
            print(f"  [DRY-RUN] 跳过执行")
            continue
        try:
            result = execute(day)
            if result:
                ok_count += 1
                executed_days.append(day)
                print(f"  ✅ {day} 执行成功")
            else:
                skip_count += 1
                print(f"  ⏭️ {day} 被跳过")
        except Exception as e:
            fail_count += 1
            print(f"  ❌ {day} 执行失败: {e}")

    print(f"\n[auto_execute] 完成: 成功={ok_count}, 跳过={skip_count}, 失败={fail_count}")

    return {
        "message": f"完成: 成功={ok_count}, 跳过={skip_count}, 失败={fail_count}",
        "pending_count": len(pending),
        "executed": executed_days,
        "ok": ok_count,
        "skip": skip_count,
        "fail": fail_count
    }


def run(ctx=None):
    """Blueprint Code Automation 入口"""
    result = _execute_pending(dry_run=False)
    return {"artifact": result}


def main():
    """本地 CLI 入口"""
    parser = argparse.ArgumentParser(description="自动执行 approved 决策")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不执行")
    args = parser.parse_args()

    result = _execute_pending(dry_run=args.dry_run)
    print(json.dumps({"artifact": result}, ensure_ascii=False))
    return 0 if result["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
