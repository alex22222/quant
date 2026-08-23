# -*- coding: utf-8 -*-
"""Paper 挂单顺延重试测试（策略库审查 P1 第 8 条：
涨停买不到、跌停卖不出后的后续重试）"""
import sqlite3
from unittest.mock import patch

import pandas as pd
import pytest

import pipeline.stage_paper as sp
from paper.execution_model import Bar

DDL = """
CREATE TABLE pending_orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_day TEXT, code TEXT, side TEXT, qty INTEGER,
    planned_price REAL, signal_source TEXT,
    status TEXT DEFAULT 'pending',
    reason TEXT,
    retries INTEGER DEFAULT 0);
"""


class _BD:
    """最小 bundle 桩：只提供 load_index 的日期。"""

    def __init__(self, day):
        self._day = day

    def load_index(self, code):
        return pd.DataFrame(index=pd.DatetimeIndex([self._day]))


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(DDL)
    return conn


def _add_buy(conn, code="AAA.XSHG", qty=100, sday="2024-01-05"):
    conn.execute("INSERT INTO pending_orders(signal_day, code, side, qty, "
                 "planned_price, signal_source) VALUES (?,?,?,?,?,?)",
                 (sday, code, "buy", qty, 10.0, "test"))
    conn.commit()


def _order(conn):
    return conn.execute("SELECT status, retries, reason FROM pending_orders "
                        "WHERE id=1").fetchone()


LIMIT_UP_BAR = Bar(open=11.0, close=11.0, prev_close=10.0)      # 开盘涨停
NORMAL_BAR = Bar(open=10.5, close=10.6, prev_close=10.0)


class TestOrderRetry:
    def test_limit_up_buy_retried_next_day(self):
        """涨停买入拒单不作废，保留 pending 顺延；打开涨停后成交"""
        conn = _conn()
        _add_buy(conn)
        positions, trades = {}, []

        with patch.object(sp, "bar_from_bundle", return_value=LIMIT_UP_BAR):
            sp._fill_pending(conn, _BD("2024-01-08"), "2024-01-08",
                             100000.0, positions, trades)
        status, retries, reason = _order(conn)
        assert status == "pending" and retries == 1
        assert "顺延" in reason

        with patch.object(sp, "bar_from_bundle", return_value=NORMAL_BAR):
            sp._fill_pending(conn, _BD("2024-01-09"), "2024-01-09",
                             100000.0, positions, trades)
        status, _, _ = _order(conn)
        assert status == "filled"
        assert positions["AAA.XSHG"]["qty"] == 100

    def test_retry_exhausted_voids_order(self):
        """连续 5 日不可成交后作废，防僵尸单"""
        conn = _conn()
        _add_buy(conn)
        positions, trades = {}, []
        with patch.object(sp, "bar_from_bundle", return_value=LIMIT_UP_BAR):
            for i in range(sp.MAX_ORDER_RETRIES + 1):
                sp._fill_pending(conn, _BD("2024-01-08"), "2024-01-08",
                                 100000.0, positions, trades)
        status, retries, reason = _order(conn)
        assert status == "rejected"
        assert retries == sp.MAX_ORDER_RETRIES
        assert "作废" in reason

    def test_logic_error_rejected_immediately(self):
        """逻辑性拒单（现金不足）不重试，立即作废"""
        conn = _conn()
        conn.execute("INSERT INTO pending_orders(signal_day, code, side, qty, "
                     "planned_price, signal_source) VALUES (?,?,?,?,?,?)",
                     ("2024-01-05", "AAA.XSHG", "buy", 99999900, 10.0, "test"))
        conn.commit()
        with patch.object(sp, "bar_from_bundle", return_value=NORMAL_BAR):
            sp._fill_pending(conn, _BD("2024-01-08"), "2024-01-08",
                             1000.0, {}, [])
        status, retries, reason = _order(conn)
        assert status == "rejected" and retries == 0
        assert "现金不足" in reason

    def test_limit_down_sell_retried(self):
        """跌停卖出拒单顺延，次日跌停打开后成交"""
        conn = _conn()
        conn.execute("INSERT INTO pending_orders(signal_day, code, side, qty, "
                     "planned_price, signal_source) VALUES (?,?,?,?,?,?)",
                     ("2024-01-05", "AAA.XSHG", "sell", 100, 10.0, "test"))
        conn.commit()
        positions = {"AAA.XSHG": {"qty": 100, "cost": 10.0, "buy_day": "2024-01-04"}}
        limit_down = Bar(open=9.0, close=9.0, prev_close=10.0)
        with patch.object(sp, "bar_from_bundle", return_value=limit_down):
            sp._fill_pending(conn, _BD("2024-01-08"), "2024-01-08",
                             50000.0, positions, [])
        assert _order(conn)[0] == "pending"
        with patch.object(sp, "bar_from_bundle", return_value=NORMAL_BAR):
            sp._fill_pending(conn, _BD("2024-01-09"), "2024-01-09",
                             50000.0, positions, [])
        assert _order(conn)[0] == "filled"
        assert positions.get("AAA.XSHG", {"qty": 0})["qty"] == 0
