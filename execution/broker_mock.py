# -*- coding: utf-8 -*-
"""MockBroker：本地模拟券商，用于 dry-run 全链路验证与实盘前演练

状态持久化在 execution/mock_broker.db（独立于 paper/paper.db——
paper 是策略线模拟台账，mock 是执行线演练环境，两者不混）。
成交语义：挂单价 ≥ 最新价（买）/ ≤ 最新价（卖）即视为立即成交，
成交价为行情最新价；否则挂单留在当日委托（未成交）。
"""
import itertools
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

TZ = timezone(timedelta(hours=8))

from .broker_base import (Balance, Broker, Entrust, OrderResult, Position,
                          to_rqalpha, to_std)
from .market import quotes

INIT_CASH = 100000.0


class MockBroker(Broker):
    name = "mock"

    def __init__(self, db_path: Path, price_fn=None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # price_fn: list[str] -> dict[num -> Quote]，默认腾讯行情
        self._price_fn = price_fn or quotes
        self._ids = itertools.count(1)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY CHECK(id=1),
            cash REAL, init_cash REAL);
        CREATE TABLE IF NOT EXISTS positions(code TEXT PRIMARY KEY,
            qty INTEGER, available INTEGER, avg_cost REAL);
        CREATE TABLE IF NOT EXISTS entrusts(order_id TEXT PRIMARY KEY,
            day TEXT, code TEXT, side TEXT, qty INTEGER, price REAL, status TEXT,
            filled_qty INTEGER, filled_price REAL);
        """)
        if self._conn.execute("SELECT COUNT(*) FROM account").fetchone()[0] == 0:
            self._conn.execute("INSERT INTO account VALUES (1, ?, ?)",
                               (INIT_CASH, INIT_CASH))
            self._conn.commit()

    # ---------- Broker 接口 ----------

    def connect(self) -> None:
        pass

    def balance(self) -> Balance:
        cash = self._conn.execute("SELECT cash FROM account WHERE id=1").fetchone()[0]
        mv = 0.0
        pos = self.positions()
        if pos:
            px = self._price_fn([p.code for p in pos])
            for p in pos:
                q = px.get(to_std(p.code))
                mv += p.qty * (q.last if q and q.last > 0 else p.avg_cost)
        return Balance(cash=cash, total_asset=cash + mv, market_value=mv)

    def positions(self) -> List[Position]:
        rows = self._conn.execute(
            "SELECT code, qty, available, avg_cost FROM positions WHERE qty > 0").fetchall()
        return [Position(code=r[0], qty=r[1], available=r[2], avg_cost=r[3])
                for r in rows]

    def buy(self, code: str, price: float, qty: int) -> OrderResult:
        return self._place("buy", code, price, qty)

    def sell(self, code: str, price: float, qty: int) -> OrderResult:
        return self._place("sell", code, price, qty)

    def cancel(self, order_id: str) -> OrderResult:
        row = self._conn.execute(
            "SELECT status FROM entrusts WHERE order_id=?", (order_id,)).fetchone()
        if not row:
            return OrderResult(ok=False, message=f"委托不存在: {order_id}")
        if row[0] != "未成交":
            return OrderResult(ok=False, message=f"状态 {row[0]} 不可撤")
        self._conn.execute("UPDATE entrusts SET status='已撤' WHERE order_id=?",
                           (order_id,))
        self._conn.commit()
        return OrderResult(ok=True, order_id=order_id, message="已撤")

    def today_entrusts(self) -> List[Entrust]:
        today = datetime.now(TZ).date().isoformat()
        rows = self._conn.execute(
            "SELECT order_id, code, side, qty, price, status, filled_qty, filled_price "
            "FROM entrusts WHERE day=?", (today,)).fetchall()
        return [Entrust(order_id=r[0], code=r[1], side=r[2], qty=r[3], price=r[4],
                        status=r[5], filled_qty=r[6], filled_price=r[7])
                for r in rows]

    def close(self) -> None:
        self._conn.close()

    # ---------- 内部 ----------

    def _place(self, side: str, code: str, price: float, qty: int) -> OrderResult:
        code = to_rqalpha(code)
        num = to_std(code)
        if qty <= 0 or qty % 100 != 0:
            return OrderResult(ok=False, message=f"数量必须为 100 的整数倍: {qty}")

        q = self._price_fn([num]).get(num)
        if not q or q.suspended:
            return OrderResult(ok=False, message=f"无行情或停牌: {code}")

        if side == "buy":
            cost = q.last * qty
            cash = self._conn.execute("SELECT cash FROM account WHERE id=1").fetchone()[0]
            if price >= q.last and cost > cash:
                return OrderResult(ok=False,
                                   message=f"资金不足: 需 {cost:.0f} 可用 {cash:.0f}")
            if price < q.last:
                return self._record(side, code, price, qty, "未成交", 0, 0.0)
            self._conn.execute("UPDATE account SET cash = cash - ? WHERE id=1", (cost,))
            old = self._conn.execute(
                "SELECT qty, avg_cost FROM positions WHERE code=?", (code,)).fetchone()
            if old:
                new_qty = old[0] + qty
                new_cost = (old[0] * old[1] + qty * q.last) / new_qty
                self._conn.execute(
                    "UPDATE positions SET qty=?, avg_cost=? WHERE code=?",
                    (new_qty, new_cost, code))  # 今日买入部分 available 不变（T+1）
            else:
                self._conn.execute(
                    "INSERT INTO positions VALUES (?,?,?,?)", (code, qty, 0, q.last))
            self._conn.commit()
            return self._record(side, code, price, qty, "已成", qty, q.last)

        # sell
        row = self._conn.execute(
            "SELECT qty, available FROM positions WHERE code=?", (code,)).fetchone()
        if not row or row[1] < qty:
            avail = row[1] if row else 0
            return OrderResult(ok=False, message=f"可卖不足: 需 {qty} 可卖 {avail}")
        if price > q.last:
            return self._record(side, code, price, qty, "未成交", 0, 0.0)
        proceeds = q.last * qty
        self._conn.execute("UPDATE account SET cash = cash + ? WHERE id=1", (proceeds,))
        self._conn.execute(
            "UPDATE positions SET qty = qty - ?, available = available - ? WHERE code=?",
            (qty, qty, code))
        self._conn.execute("DELETE FROM positions WHERE qty <= 0")
        self._conn.commit()
        return self._record(side, code, price, qty, "已成", qty, q.last)

    def _record(self, side, code, price, qty, status, filled_qty, filled_price):
        oid = f"MOCK-{next(self._ids):06d}"
        today = datetime.now(TZ).date().isoformat()
        self._conn.execute(
            "INSERT INTO entrusts VALUES (?,?,?,?,?,?,?,?,?)",
            (oid, today, code, side, qty, price, status, filled_qty, filled_price))
        self._conn.commit()
        return OrderResult(ok=(status == "已成"), order_id=oid, message=status)

    def roll_to_next_day(self) -> None:
        """日终：持仓全部转为可卖（模拟 T+1 解冻），清理未成交挂单"""
        self._conn.execute("UPDATE positions SET available = qty")
        self._conn.execute("DELETE FROM entrusts WHERE status='未成交'")
        self._conn.commit()
