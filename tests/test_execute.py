# -*- coding: utf-8 -*-
"""trading_team/execute.py：执行器门禁、仓位调整、权益守恒、幂等"""
import json
import sqlite3

import pytest

from trading_team import execute as ex
from paper.execution_model import Bar

PLAN_HASH = "a" * 64


@pytest.fixture
def env(tmp_path, monkeypatch, stub_research_reviews):
    dec_dir = tmp_path / "decisions" / "2026-08-23"
    dec_dir.mkdir(parents=True)
    monkeypatch.setattr(ex, "DECISIONS_DIR", tmp_path / "decisions")
    monkeypatch.setattr(ex, "DB", tmp_path / "paper" / "paper.db")
    monkeypatch.setattr(ex, "ROOT", tmp_path)  # team.db 落到临时目录（不存在则跳过写入）
    # 一致性校验通过（单独由 test_consistency 覆盖）
    monkeypatch.setattr(ex.consistency_check, "check_day",
                        lambda day: {"ok": True, "conflicts": [], "warnings": []})
    bars = {
        "601318": Bar(open=50.0, high=51.0, low=49.0, close=50.0, prev_close=49.5),
        "600036": Bar(open=40.0, high=41.0, low=39.0, close=40.0, prev_close=39.5),
    }
    monkeypatch.setattr(ex, "load_execution_bars",
                        lambda day, codes, team: {c: bars[c] for c in codes})
    return tmp_path


def _write_decision(tmp_path, plans, pm_status="approved", market_ok=True):
    dec = {"date": "2026-08-23", "market_ok": market_ok,
           "pm_status": pm_status, "plan_hash": PLAN_HASH, "plans": plans}
    p = tmp_path / "decisions" / "2026-08-23" / "decision.json"
    p.write_text(json.dumps(dec, ensure_ascii=False), encoding="utf-8")


def _plan(code, direction, pct, approval="approved", executable=True, **kw):
    return {"code": code, "name": code, "direction": direction,
            "position_pct": pct, "approval": approval,
            "approved_plan_hash": PLAN_HASH, "executable": executable,
            "entry": kw.pop("entry", [50.0 if code == "601318" else 40.0]),
            "entry_type": kw.pop("entry_type", "limit"), **kw}


def _account(day="2026-08-23"):
    if not ex.DB.exists():
        return [], [], []
    conn = sqlite3.connect(ex.DB)
    rows = conn.execute("SELECT day, cash, equity FROM account").fetchall()
    pos = conn.execute("SELECT code, qty FROM positions").fetchall()
    trades = conn.execute("SELECT day, code, side, qty, price, amount FROM trades WHERE day=?", (day,)).fetchall()
    orders = conn.execute("SELECT code, status, reason, fee FROM pending_orders").fetchall()
    conn.close()
    return rows, pos, trades, orders


class TestGating:
    def test_dry_run_writes_nothing(self, env):
        _write_decision(env, [_plan("601318", "买入", 0.15)])
        assert ex.execute("2026-08-23", confirm=False) is True
        assert not ex.DB.exists() or _account()[2] == []

    def test_pending_plan_not_executed(self, env):
        _write_decision(env, [_plan("601318", "买入", 0.15,
                                    approval="pending", executable=False)])
        assert ex.execute("2026-08-23", confirm=True) is True
        assert _account()[2] == []

    def test_consistency_conflict_blocks(self, env, monkeypatch):
        _write_decision(env, [_plan("601318", "买入", 0.15)])
        monkeypatch.setattr(ex.consistency_check, "check_day",
                            lambda day: {"ok": False, "conflicts": ["x"], "warnings": []})
        assert ex.execute("2026-08-23", confirm=True) is False
        assert _account()[2] == []

    def test_unapproved_pm_status_skips(self, env):
        _write_decision(env, [_plan("601318", "买入", 0.15)], pm_status="pending")
        assert ex.execute("2026-08-23", confirm=True) is False


class TestTrading:
    @pytest.mark.parametrize("closing_price", [36, 44])
    def test_sizing_includes_non_actionable_holdings_at_pretrade_reference(self, env, monkeypatch, closing_price):
        with ex._db() as conn:
            conn.execute("UPDATE account SET cash=92000")
            conn.execute("INSERT INTO positions VALUES ('600036',200,40,'2026-08-22')")
            conn.execute("INSERT INTO trades(day,code,side,qty) VALUES ('2026-08-22','600036','buy',200)")
        _write_decision(env, [_plan("601318", "买入", 0.1)])
        bars = {
            "601318": Bar(open=50, high=51, low=49, close=50, prev_close=50),
            "600036": Bar(open=40, high=44, low=36, close=closing_price, prev_close=40),
        }
        monkeypatch.setattr(ex, "load_execution_bars", lambda day, codes, team: {c: bars[c] for c in codes})
        assert ex.execute("2026-08-23", confirm=True)
        assert _account()[2][0][3] == 200

    def test_buy_position_sizing(self, env):
        _write_decision(env, [_plan("601318", "买入", 0.15)])
        ex.execute("2026-08-23", confirm=True)
        _, pos, trades, orders = _account()
        # 15% of 100000 = 15000 → 15000/50 = 300 股（整手）
        assert pos == [("601318", 300)]
        # 开盘恰好等于买入限价，不利滑点不得突破限价。
        assert trades[0][2:] == ("buy", 300, 50.0, 15000.0)
        assert orders[0][1] == "filled" and orders[0][3] > 0

    def test_cash_shortage_adjusts_qty(self, env):
        _write_decision(env, [_plan("601318", "买入", 0.6),
                              _plan("600036", "买入", 0.6)])
        ex.execute("2026-08-23", confirm=True)
        _, pos, trades, _ = _account()
        assert dict(pos) == {"601318": 1200, "600036": 900}

    def test_reduced_halves_position(self, env):
        _write_decision(env, [_plan("601318", "买入", 0.30, approval="reduced")])
        ex.execute("2026-08-23", confirm=True)
        _, pos, _, _ = _account()
        assert pos == [("601318", 300)]  # 0.15 * 100000 / 50

    def test_equity_conservation(self, env):
        """positions 重写前后：cash + 持仓市值 = 权益（无费用时严格相等）"""
        _write_decision(env, [_plan("601318", "买入", 0.15)])
        ex.execute("2026-08-23", confirm=True)
        rows, pos, trades, _ = _account()
        cash = rows[-1][1]
        equity = rows[-1][2]
        mv = sum(qty * 50.0 for _, qty in pos)
        assert abs(cash + mv - equity) < 0.01

    def test_idempotent_second_run(self, env):
        _write_decision(env, [_plan("601318", "买入", 0.15)])
        ex.execute("2026-08-23", confirm=True)
        ex.execute("2026-08-23", confirm=True)
        _, pos, trades, orders = _account()
        assert len(trades) == 1 and dict(pos) == {"601318": 300}
        assert len(orders) == 1

    def test_string_pct_fails_closed(self, env):
        _write_decision(env, [_plan("601318", "买入", "15%（分批）")])
        assert ex.execute("2026-08-23", confirm=True) is True  # 不崩，但该计划被拒
        assert _account()[2] == []

    def test_untouched_limit_is_persisted_unfilled(self, env):
        _write_decision(env, [_plan("601318", "条件买入", 0.15, entry=[48.0])])
        assert ex.execute("2026-08-23", confirm=True)
        _, pos, trades, orders = _account()
        assert pos == [] and trades == []
        assert orders[0][1] == "unfilled"

    @pytest.mark.parametrize("direction,qty,remaining", [("减持", 100, 200), ("卖出", "all", 0), ("回避", None, 300)])
    def test_explicit_exit_is_not_an_implicit_full_liquidation(self, env, direction, qty, remaining):
        with ex._db() as conn:
            conn.execute("UPDATE account SET cash=85000")
            conn.execute("INSERT INTO positions VALUES ('601318',300,50,'2026-08-22')")
            conn.execute("INSERT INTO trades(day,code,side,qty) VALUES ('2026-08-22','601318','buy',300)")
        plan = _plan("601318", direction, 0, entry=None, entry_type=None)
        if qty is not None:
            plan["exit"] = {"type": "market", "qty": qty, "time_in_force": "DAY"}
        _write_decision(env, [plan])
        assert ex.execute("2026-08-23", confirm=True)
        _, positions, trades, _ = _account()
        assert dict(positions).get("601318", 0) == remaining
        if remaining == 300:
            assert trades == []

    def test_legacy_exit_limit_is_not_replaced_by_market_sale(self, env):
        with ex._db() as conn:
            conn.execute("UPDATE account SET cash=85000")
            conn.execute("INSERT INTO positions VALUES ('601318',300,50,'2026-08-22')")
            conn.execute("INSERT INTO trades(day,code,side,qty) VALUES ('2026-08-22','601318','buy',300)")
        plan = _plan("601318", "卖出", 0, entry=None, entry_type=None,
                     exit={"type": "limit", "qty": "all", "price": 52, "time_in_force": "DAY"})
        _write_decision(env, [plan])
        assert ex.execute("2026-08-23", confirm=True)
        assert _account()[3][0][1] == "unfilled"
        assert _account()[2] == []
