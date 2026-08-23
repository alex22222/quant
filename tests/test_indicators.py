# -*- coding: utf-8 -*-
"""trading_team/collect.py compute_indicators：Stochastic/VWAP/布林带（quant-wiki 建议 2）"""
from trading_team.collect import compute_indicators


def _rows(closes, vol=1000):
    """由收盘价序列构造最小行情行（open=前收，high/low ±1%）。"""
    rows = []
    for i, c in enumerate(closes):
        rows.append({"date": f"2024-01-{i+1:02d}", "open": closes[i - 1] if i else c,
                     "close": c, "high": c * 1.01, "low": c * 0.99,
                     "volume": vol, "pct_chg": 0, "turnover": 1.0})
    return rows


class TestNewIndicators:
    def test_uptrend_stoch_vwap_boll(self):
        """单边上行：%K 高位、收盘在 VWAP 上方、%B 接近 1"""
        rows = _rows([10 + i * 0.1 for i in range(60)])
        ind = compute_indicators(rows)
        assert ind["stoch_k"] > 80
        assert ind["stoch_d"] is not None
        assert ind["close"] > ind["vwap_20d"]
        assert ind["bollinger"]["pct_b"] > 0.8
        assert ind["bollinger"]["upper"] > ind["bollinger"]["mid"] > ind["bollinger"]["lower"]

    def test_flat_market(self):
        """完全横盘：%K=50（无区间）、VWAP=收盘、布林收口 %B 为 None"""
        rows = _rows([10.0] * 60)
        ind = compute_indicators(rows)
        assert ind["stoch_k"] == 50.0
        assert ind["vwap_20d"] == 10.0
        assert ind["bollinger"]["pct_b"] is None  # upper==lower，防除零

    def test_downtrend_stoch_low(self):
        """单边下行：%K 低位、收盘在 VWAP 下方"""
        rows = _rows([20 - i * 0.1 for i in range(60)])
        ind = compute_indicators(rows)
        assert ind["stoch_k"] < 20
        assert ind["close"] < ind["vwap_20d"]

    def test_short_series_fail_closed(self):
        """数据不足时新指标为 None 而非报错"""
        rows = _rows([10.0] * 10)
        ind = compute_indicators(rows)
        assert ind["stoch_k"] is None and ind["vwap_20d"] is None
        assert ind["bollinger"]["mid"] is None
