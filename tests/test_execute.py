# -*- coding: utf-8 -*-
"""trading_team/execute.py：执行器门禁、仓位调整、权益守恒、幂等"""
import json
import sqlite3

import pytest

from trading_team import execute as ex


@pytest.fixture
def env(tmp_path, monkeypatch):
    dec_dir = tmp_path / "decisions" / "2026-08-23"
    dec_dir.mkdir(parents=True)
    monkeypatch.setattr(ex, "DECISIONS_DIR", tmp_path / "decisions")
    monkeypatch.setattr(ex, "DB", tmp_path / "paper" / "paper.db")
    monkeypatch.setattr(ex, "ROOT", tmp_path)  # team.db 落到临时目录（不存在则跳过写入）
    # 一致性校验通过（单独由 test_consistency 覆盖）
    monkeypatch.setattr(ex.consistency_check, "check_day",
                        lambda day: {"ok": True, "conflicts": [], "warnings": []})
    # 固定价格源
    prices = {"601318": 50.0, "600036": 40.0}
    monkeypatch.setattr(ex, "load_close_price", lambda code, day: prices.get(code, 10.0))
    return tmp_path


def _write_decision(tmp_path, plans, pm_status="approved", market_ok=True):
    dec = {"date": "2026-08-23", "market_ok": market_ok,
           "pm_status": pm_status, "plans": plans}
    p = tmp_path / "decisions" / "2026-08-23" / "decision.json"
    p.write_text(json.dumps(dec, ensure_ascii=False), encoding="utf-8")


def _plan(code, direction, pct, approval="approved", executable=True, **kw):
    return {"code": code, "name": code, "direction": direction,
            "position_pct": pct, "approval": approval,
            "executable": executable, **kw}


def _account(day="2026-08-23"):
    if not ex.DB.exists():
        return [], [], []
    conn = sqlite3.connect(ex.DB)
    rows = conn.execute("SELECT day, cash, equity FROM account").fetchall()
    pos = conn.execute("SELECT code, qty FROM positions").fetchall()
    trades = conn.execute("SELECT day, code, side, qty, price, amount FROM trades").fetchall()
    conn.close()
    return rows, pos, trades


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
    def test_buy_position_sizing(self, env):
        _write_decision(env, [_plan("601318", "买入", 0.15)])
        ex.execute("2026-08-23", confirm=True)
        _, pos, trades = _account()
        # 15% of 100000 = 15000 → 15000/50 = 300 股（整手）
        assert pos == [("601318", 300)]
        assert trades[0][2:] == ("buy", 300, 50.0, 15000.0)

    def test_cash_shortage_adjusts_qty(self, env):
        _write_decision(env, [_plan("601318", "买入", 0.6),
                              _plan("600036", "买入", 0.6)])
        ex.execute("2026-08-23", confirm=True)
        _, pos, trades = _account()
        # 第一笔 60000 → 1200 股；第二笔目标 60000 但现金 40000 → 1000 股
        assert dict(pos) == {"601318": 1200, "600036": 1000}

    def test_reduced_halves_position(self, env):
        _write_decision(env, [_plan("601318", "买入", 0.30, approval="reduced")])
        ex.execute("2026-08-23", confirm=True)
        _, pos, _ = _account()
        assert pos == [("601318", 300)]  # 0.15 * 100000 / 50

    def test_equity_conservation(self, env):
        """positions 重写前后：cash + 持仓市值 = 权益（无费用时严格相等）"""
        _write_decision(env, [_plan("601318", "买入", 0.15)])
        ex.execute("2026-08-23", confirm=True)
        rows, pos, trades = _account()
        cash = rows[-1][1]
        equity = rows[-1][2]
        mv = sum(qty * 50.0 for _, qty in pos)
        assert abs(cash + mv - equity) < 0.01

    def test_idempotent_second_run(self, env):
        _write_decision(env, [_plan("601318", "买入", 0.15)])
        ex.execute("2026-08-23", confirm=True)
        ex.execute("2026-08-23", confirm=True)
        _, pos, trades = _account()
        assert len(trades) == 1 and dict(pos) == {"601318": 300}

    def test_string_pct_fails_closed(self, env):
        _write_decision(env, [_plan("601318", "买入", "15%（分批）")])
        assert ex.execute("2026-08-23", confirm=True) is True  # 不崩，但该计划被拒
        assert _account()[2] == []
