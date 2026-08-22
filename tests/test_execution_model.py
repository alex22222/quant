# -*- coding: utf-8 -*-
"""paper/execution_model.py：A 股撮合约束与费用"""
from paper.execution_model import (
    Bar, FeeConfig, is_limit_down, is_limit_up, round_lot, simulate_fill,
    total_fee,
)

CFG = FeeConfig()


class TestFees:
    def test_min_commission(self):
        # 1 万元买入：佣金 10000*0.00025=2.5 < 5 → 收 5；过户费 0.2
        assert total_fee("buy", 10000, CFG) == 5.0 + 0.2

    def test_sell_has_stamp_tax(self):
        # 10 万卖出：佣金 25 + 印花税 50 + 过户费 2
        assert total_fee("sell", 100000, CFG) == 25.0 + 50.0 + 2.0

    def test_buy_no_stamp_tax(self):
        assert total_fee("buy", 100000, CFG) == 25.0 + 2.0


class TestLot:
    def test_round_lot(self):
        assert round_lot(150) == 100
        assert round_lot(99) == 0
        assert round_lot(260) == 200


class TestLimits:
    def test_limit_up_buy_rejected(self):
        bar = Bar(open=11.0, close=11.0, prev_close=10.0)  # +10% 涨停
        assert is_limit_up(bar)
        f = simulate_fill("buy", 100, bar, CFG)
        assert not f.filled and "涨停" in f.reject_reason

    def test_limit_down_sell_rejected(self):
        bar = Bar(open=9.0, close=9.0, prev_close=10.0)  # -10% 跌停
        assert is_limit_down(bar)
        f = simulate_fill("sell", 100, bar, CFG)
        assert not f.filled and "跌停" in f.reject_reason

    def test_limit_up_sell_allowed(self):
        bar = Bar(open=11.0, close=11.0, prev_close=10.0)
        f = simulate_fill("sell", 100, bar, CFG)
        assert f.filled and f.price == 11.0

    def test_suspended_rejected(self):
        f = simulate_fill("buy", 100, Bar(suspended=True), CFG)
        assert not f.filled

    def test_next_open_fill(self):
        bar = Bar(open=10.5, close=10.8, prev_close=10.4)
        f = simulate_fill("buy", 250, bar, CFG)
        assert f.filled and f.qty == 200 and f.price == 10.5
        assert f.price_type == "next_open" and f.fee > 0

    def test_fallback_close(self):
        bar = Bar(open=None, close=10.8, prev_close=10.4)
        f = simulate_fill("buy", 100, bar, CFG)
        assert f.filled and f.price_type == "fallback_close"

    def test_gem_20pct_limit(self):
        bar = Bar(open=11.5, close=11.5, prev_close=10.0, limit_pct=0.20)
        assert not is_limit_up(bar)  # 创业板 +15% 未涨停
