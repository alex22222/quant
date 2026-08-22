# -*- coding: utf-8 -*-
"""execution_model.compare_stop_fills：止损三口径对比"""
from paper.execution_model import Bar, compare_stop_fills


class TestStopComparison:
    def test_normal_next_open_fill(self):
        sig = Bar(open=10.0, close=9.8, prev_close=10.1)   # 收盘跌破止损 10.0
        nxt = Bar(open=9.5, close=9.6, prev_close=9.8)
        r = compare_stop_fills("601318.XSHG", 10.0, 100, sig, nxt)
        assert r["close_trigger"].filled and r["close_trigger"].price == 9.8
        assert r["next_open"].filled and r["next_open"].price == 9.5
        assert r["worst_slippage"] < 0  # 次日开盘比收盘更差

    def test_limit_down_delays_stop(self):
        sig = Bar(open=10.0, close=9.8, prev_close=10.1)
        nxt = Bar(open=8.82, close=8.82, prev_close=9.8)   # -10% 跌停
        r = compare_stop_fills("601318.XSHG", 10.0, 100, sig, nxt)
        assert r["close_trigger"].filled
        assert not r["next_open"].filled
        assert "延迟" in r["delayed_limit"].reject_reason

    def test_no_trigger(self):
        sig = Bar(open=10.2, close=10.3, prev_close=10.1)  # 收盘在止损上方
        nxt = Bar(open=10.2, close=10.2, prev_close=10.3)
        r = compare_stop_fills("601318.XSHG", 10.0, 100, sig, nxt)
        assert not r["close_trigger"].filled
        assert r["worst_slippage"] == 0.0
