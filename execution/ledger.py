# -*- coding: utf-8 -*-
"""ledger：执行线 SQLite 台账（execution/execution.db）

与 paper/paper.db 分离：paper 记策略线模拟账，本库记实盘/演练通道的
真实委托与成交。事实源纪律同 AGENT.md。
"""
import sqlite3
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT, ts TEXT, code TEXT, side TEXT, qty INTEGER, price REAL,
    mode TEXT,                -- dry-run / live
    broker_order_id TEXT, status TEXT, message TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS daily(
    day TEXT PRIMARY KEY, mode TEXT, equity REAL, cash REAL,
    orders_sent INTEGER, orders_filled INTEGER, note TEXT);
"""


class Ledger:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(SCHEMA)

    def orders_today(self, day: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM orders WHERE day=? AND status != 'blocked'",
            (day,)).fetchone()[0]

    def prev_equity(self, day: str) -> Optional[float]:
        row = self.conn.execute(
            "SELECT equity FROM daily WHERE day < ? ORDER BY day DESC LIMIT 1",
            (day,)).fetchone()
        return row[0] if row else None

    def has_run(self, day: str, mode: str) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM daily WHERE day=? AND mode=?", (day, mode)).fetchone())

    def log_order(self, day, ts, spec, mode, result, reason=""):
        self.conn.execute(
            "INSERT INTO orders(day,ts,code,side,qty,price,mode,broker_order_id,status,message,reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (day, ts, spec.code, spec.side, spec.qty, spec.price, mode,
             result.order_id, "filled" if result.ok else "rejected",
             result.message[:200], reason or spec.reason))
        self.conn.commit()

    def log_blocked(self, day, ts, item, mode):
        self.conn.execute(
            "INSERT INTO orders(day,ts,code,side,qty,price,mode,broker_order_id,status,message,reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (day, ts, item["code"], item["side"], item["qty"], 0.0, mode,
             "", "blocked", "", item["reason"]))
        self.conn.commit()

    def close_day(self, day, mode, equity, cash, sent, filled, note=""):
        self.conn.execute(
            "INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?)",
            (day, mode, equity, cash, sent, filled, note))
        self.conn.commit()

    def close(self):
        self.conn.close()
