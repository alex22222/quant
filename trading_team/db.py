#!/usr/bin/env python3
"""
trading_team/team.db —— LLM 交易团队数据中枢

表结构：
- decisions:    每日决策主表（来自 decisions/YYYY-MM-DD/decision.json）
- plans:        交易计划明细（来自 decision.json 中的 plans）
- execution_log: 执行日志（来自 execute.py）
- accuracy:     准确率追踪（来自 accuracy.json）

用法：
    python trading_team/db.py init          # 初始化数据库
    python trading_team/db.py migrate       # 迁移现有 JSON 数据
    python trading_team/db.py status        # 查看待执行决策
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "trading_team" / "team.db"
DECISIONS_DIR = ROOT / "trading_team" / "decisions"
LOOPS_DIR = ROOT / "trading_team" / "loops"


def get_conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化数据库表结构"""
    conn = get_conn()
    conn.executescript("""
    -- 决策主表
    CREATE TABLE IF NOT EXISTS decisions (
        date TEXT PRIMARY KEY,
        pm_status TEXT DEFAULT 'pending',
        market_ok BOOLEAN,
        trader_summary TEXT,
        risk_verdict TEXT,
        created_at TEXT,
        executed_at TEXT,
        execution_result TEXT
    );

    -- 交易计划明细
    CREATE TABLE IF NOT EXISTS plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_date TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT,
        direction TEXT,
        position_pct REAL,
        entry TEXT,
        stop REAL,
        invalid_if TEXT,
        horizon TEXT,
        confidence INTEGER,
        FOREIGN KEY (decision_date) REFERENCES decisions(date)
    );

    -- 执行日志
    CREATE TABLE IF NOT EXISTS execution_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_date TEXT NOT NULL,
        code TEXT,
        side TEXT,
        qty INTEGER,
        price REAL,
        amount REAL,
        reason TEXT,
        executed_at TEXT DEFAULT (datetime('now', 'localtime'))
    );

    -- 准确率追踪
    CREATE TABLE IF NOT EXISTS accuracy (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_date TEXT NOT NULL,
        code TEXT NOT NULL,
        direction TEXT,
        predicted TEXT,
        actual TEXT,
        score REAL,
        verified_at TEXT
    );

    -- 审批记录（替代 approvals.json）
    CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_date TEXT NOT NULL,
        code TEXT,
        decision_type TEXT,  -- approved / rejected / reduced
        decided_at TEXT DEFAULT (datetime('now', 'localtime')),
        source TEXT,
        note TEXT
    );
    """)
    conn.commit()
    conn.close()
    print(f"[OK] 数据库初始化完成: {DB_PATH}")


def migrate():
    """迁移现有 JSON 数据到 SQLite"""
    conn = get_conn()

    # 1. 迁移 accuracy.json
    acc_path = LOOPS_DIR / "accuracy.json"
    if acc_path.exists():
        with open(acc_path, "r", encoding="utf-8") as f:
            acc = json.load(f)
        for item in acc.get("records", []):
            conn.execute("""
                INSERT OR IGNORE INTO accuracy
                (decision_date, code, direction, predicted, actual, score, verified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                item.get("date"), item.get("code"), item.get("direction"),
                item.get("predicted"), item.get("actual"), item.get("score"),
                item.get("verified_at")
            ))
        print(f"[OK] accuracy.json 迁移完成: {len(acc.get('records', []))} 条")

    # 2. 迁移 decisions/ 下的 decision.json
    migrated = 0
    for dec_dir in sorted(DECISIONS_DIR.iterdir()):
        if not dec_dir.is_dir():
            continue
        dec_path = dec_dir / "decision.json"
        if not dec_path.exists():
            continue
        with open(dec_path, "r", encoding="utf-8") as f:
            dec = json.load(f)

        date = dec.get("date", dec_dir.name)
        conn.execute("""
            INSERT OR REPLACE INTO decisions
            (date, pm_status, market_ok, trader_summary, risk_verdict, created_at, executed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            date,
            dec.get("pm_status", "pending"),
            dec.get("market_ok"),
            dec.get("trader_summary", ""),
            dec.get("risk_verdict", ""),
            date,
            None  # executed_at 由 execute.py 更新
        ))

        # 迁移 plans
        for plan in dec.get("plans", []):
            conn.execute("""
                INSERT OR REPLACE INTO plans
                (decision_date, code, name, direction, position_pct, entry, stop, invalid_if, horizon, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date,
                plan.get("code"),
                plan.get("name"),
                plan.get("direction"),
                plan.get("position_pct"),
                str(plan.get("entry")) if plan.get("entry") is not None else None,
                plan.get("stop"),
                plan.get("invalid_if"),
                plan.get("horizon"),
                plan.get("confidence")
            ))
        migrated += 1

    conn.commit()
    conn.close()
    print(f"[OK] decisions/ 迁移完成: {migrated} 个决策")


def status():
    """查看待执行决策状态"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT date, pm_status, executed_at,
               (SELECT COUNT(*) FROM plans WHERE decision_date = decisions.date) as plan_count
        FROM decisions
        ORDER BY date DESC
        LIMIT 10
    """).fetchall()

    print(f"\n{'日期':<12} {'状态':<10} {'已执行':<8} {'计划数':<6}")
    print("-" * 40)
    for r in rows:
        executed = "✅" if r["executed_at"] else "⏳"
        print(f"{r['date']:<12} {r['pm_status']:<10} {executed:<8} {r['plan_count']:<6}")

    pending = conn.execute("""
        SELECT COUNT(*) FROM decisions
        WHERE pm_status = 'approved' AND executed_at IS NULL
    """).fetchone()[0]
    print(f"\n待执行: {pending} 个")
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "init":
        init_db()
    elif cmd == "migrate":
        init_db()
        migrate()
    elif cmd == "status":
        status()
    else:
        print(f"未知命令: {cmd}")
        print("用法: python db.py [init|migrate|status]")
