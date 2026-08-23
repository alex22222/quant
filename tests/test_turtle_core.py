# -*- coding: utf-8 -*-
"""策略核心行为测试（策略库审查 P1：补上策略层测试空白）

覆盖审查清单：
1. 突破不成立时不得入场
2. 同时出现多个候选时排序确定
3. market_ok=false 时持仓行为符合设计（不新开仓、不强平已有持仓）
4. 数据不足时 fail closed
5. 回测适配器与 Paper 适配器对同一数据产生相同目标
6. 权益曲线滤波在阈值上下的仓位变化
7. T 日信号只能 T+1 成交（源码结构守卫：下单只允许出现在 open_auction）
8. 出场通道：跌破 N 日低点 / ATR 止损
"""
import ast
from pathlib import Path

import numpy as np
import pytest

from strategies.turtle_bluechip import PARAMS, _effective_cap, compute_targets, generate_targets

ROOT = Path(__file__).resolve().parent.parent

# 已完成 T+1 开盘成交改造的日线策略（demo_dual_ma 为退役教学策略，除外）
DAILY_STRATEGIES = [
    "turtle_bluechip", "momentum_rotation", "momentum_stops", "sharpe_momentum",
    "week52_momentum", "ma_trend_bluechip", "rsi2_reversal", "rotation_300_500",
]
ORDER_APIS = {"order_target_percent", "order", "order_shares", "order_value",
              "order_market", "order_limit"}


def _bars(closes, opens=None, highs=None, lows=None):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    return {
        "open": np.asarray(opens, dtype=float) if opens is not None else closes.copy(),
        "high": np.asarray(highs, dtype=float) if highs is not None else closes * 1.01,
        "low": np.asarray(lows, dtype=float) if lows is not None else closes * 0.99,
        "close": closes,
    }


def _params(**over):
    p = dict(PARAMS)
    p["universe"] = ["AAA.XSHG", "BBB.XSHG", "CCC.XSHG"]
    p["hold_num"] = 2
    p.update(over)
    return p


def _index_bull(n=80):   # 缓升指数：收盘 > MA60 → market_ok=True
    return np.linspace(100, 120, n)


def _index_bear(n=80):   # 阴跌指数：收盘 < MA60 → market_ok=False
    return np.linspace(120, 100, n)


def _breakout_bars():
    """59 天平稳 + 最后一日放量阳线突破（收 10.2 > 前 25 日最高 10.1）。"""
    closes = [10.0] * 59 + [10.2]
    opens = [10.0] * 58 + [10.0, 10.05]
    highs = [10.1] * 59 + [10.25]
    lows = [9.9] * 60
    return _bars(closes, opens, highs, lows)


class TestEntry:
    def test_no_breakout_no_entry(self):
        """突破不成立（横盘）时不得入场"""
        bars = {c: _bars([10.0] * 60) for c in _params()["universe"]}
        sig = compute_targets(bars, _index_bull(), _params(), {})
        assert sig.targets == []
        assert sig.detail["entries"] == []

    def test_breakout_entry(self):
        """有效突破（收>前高、阳线、上影线<4%、MA20 上方、大盘向上）入场"""
        bars = {c: _bars([10.0] * 60) for c in _params()["universe"]}
        bars["AAA.XSHG"] = _breakout_bars()
        sig = compute_targets(bars, _index_bull(), _params(), {})
        assert sig.targets == ["AAA.XSHG"]
        assert sig.detail["entries"][0]["code"] == "AAA.XSHG"

    def test_multi_candidates_deterministic(self):
        """多只同时突破：按 universe 顺序确定性截取，两次调用结果一致

        （当前设计为 universe 顺序；按突破强度排序的改进在参数冻结期后排队，
        此测试用于钉住"确定性"而非某种特定排序）"""
        bars = {c: _breakout_bars() for c in _params()["universe"]}
        p = _params()
        sig1 = compute_targets(bars, _index_bull(), p, {})
        sig2 = compute_targets(bars, _index_bull(), p, {})
        assert sig1.targets == sig2.targets
        assert len(sig1.targets) == p["hold_num"] == 2
        assert sig1.targets == ["AAA.XSHG", "BBB.XSHG"]


class TestMarketFilter:
    def test_market_not_ok_blocks_entry_keeps_positions(self):
        """market_ok=false：不开新仓，但已有持仓不强制清掉（设计语义）"""
        bars = {c: _bars([10.0] * 60) for c in _params()["universe"]}
        sig = compute_targets(bars, _index_bear(), _params(), {"AAA.XSHG": 9.0})
        assert sig.market_ok is False
        assert sig.targets == ["AAA.XSHG"]   # 持仓保留
        assert sig.detail["entries"] == []   # 无新入场


class TestFailClosed:
    def test_insufficient_data_no_action(self):
        """数据不足时 fail closed：持仓不动、候选不入场"""
        bars = {c: _bars([10.0] * 60) for c in _params()["universe"]}
        bars["AAA.XSHG"] = _bars([10.0] * 5)   # 持仓股数据不足
        bars["BBB.XSHG"] = _bars([10.0] * 5)   # 候选股数据不足
        sig = compute_targets(bars, _index_bull(), _params(), {"AAA.XSHG": 9.0})
        assert sig.targets == ["AAA.XSHG"]     # 不动，而非误判离场
        assert "BBB.XSHG" not in sig.targets


class TestExit:
    def test_exit_channel_break(self):
        """收盘跌破前 10 日最低价 → 离场"""
        p = _params()
        bars = {c: _bars([10.0] * 60) for c in p["universe"]}
        closes = [10.0] * 59 + [9.8]           # 收于 9.8 < 前低 9.9
        bars["AAA.XSHG"] = _bars(closes, lows=[9.9] * 59 + [9.7])
        sig = compute_targets(bars, _index_bull(), p, {"AAA.XSHG": 9.0})
        assert "AAA.XSHG" not in sig.targets
        assert any("跌破" in e["reason"] for e in sig.detail["exits"])

    def test_atr_stop(self):
        """收盘跌破 成本 - 2×ATR → 止损离场"""
        p = _params()
        bars = {c: _bars([10.0] * 60, lows=[9.5] * 60) for c in p["universe"]}
        # 成本 12，ATR≈0.15 → 止损线≈11.7 > 现价 10，但前 10 日低点 9.5 不触发通道
        sig = compute_targets(bars, _index_bull(), p, {"AAA.XSHG": 12.0})
        assert "AAA.XSHG" not in sig.targets
        assert any("ATR" in e["reason"] for e in sig.detail["exits"])


class TestEquityFilter:
    def test_below_ma_shrinks_cap(self):
        """净值低于 120 日均线 → 仓位上限减半"""
        p = _params()
        nav = [1.0] * 119 + [0.95]
        assert _effective_cap(p, nav, market_weak=True) == pytest.approx(
            p["weight_cap"] * p["equity_filter_scale"])

    def test_above_ma_keeps_cap(self):
        """净值在均线上方 → 上限不变"""
        p = _params()
        nav = [1.0] * 119 + [1.05]
        assert _effective_cap(p, nav, market_weak=True) == pytest.approx(p["weight_cap"])

    def test_strong_bull_exemption(self):
        """强牛（指数高于 MA60 超 bull_band）时即使净值弱也不收缩"""
        p = _params()
        nav = [1.0] * 119 + [0.90]
        assert _effective_cap(p, nav, market_weak=False) == pytest.approx(p["weight_cap"])

    def test_weight_cap_in_signal_detail(self):
        """compute_targets 的 detail.weight_cap 随滤波变化"""
        p = _params()
        bars = {c: _bars([10.0] * 60) for c in p["universe"]}
        index = _index_bull()  # 100→120：末值 120 vs MA≈110，超 2% → 强牛豁免
        sig = compute_targets(bars, index, p, {},
                              nav_hist=[1.0] * 119 + [0.90])
        assert sig.detail["weight_cap"] == pytest.approx(p["weight_cap"])
        index_flat = np.full(80, 100.0)  # 非强牛 → 滤波生效
        sig2 = compute_targets(bars, index_flat, p, {},
                               nav_hist=[1.0] * 119 + [0.90])
        assert sig2.detail["weight_cap"] == pytest.approx(
            p["weight_cap"] * p["equity_filter_scale"])


class _FakeData:
    """最小 StrategyData 桩：与回测适配器喂入完全相同的数据。"""

    def __init__(self, bars_map, index):
        self._bars, self._index = bars_map, index

    def is_suspended(self, code):
        return False

    def ohlc(self, code, n):
        return self._bars[code]

    def index_closes(self, code, n):
        return self._index


class TestAdapterParity:
    def test_paper_and_backtest_same_targets(self):
        """回测（compute_targets）与 Paper（generate_targets）同数据同目标"""
        p = _params()
        bars = {c: _bars([10.0] * 60) for c in p["universe"]}
        bars["AAA.XSHG"] = _breakout_bars()
        index = _index_bull()
        held = {"BBB.XSHG": 9.0}

        direct = compute_targets(bars, index, p, held, nav_hist=[1.0] * 130)

        # universe 由模块 PARAMS 决定，须替换为同一小池再比
        import strategies.turtle_bluechip as tb
        orig = tb.PARAMS["universe"]
        tb.PARAMS["universe"] = p["universe"]
        try:
            via_paper = generate_targets(
                _FakeData(bars, index), params={}, nav_hist=[1.0] * 130,
                positions={"BBB.XSHG": {"qty": 100, "cost": 9.0}})
        finally:
            tb.PARAMS["universe"] = orig
        assert via_paper.targets == direct.targets
        assert via_paper.market_ok == direct.market_ok
        assert via_paper.detail["weight_cap"] == pytest.approx(direct.detail["weight_cap"])


class TestT1ExecutionGuard:
    """口径守卫（审查第 2 条）：日线策略下单只允许出现在 open_auction，
    handle_bar/rebalance 内禁止直接下单（当日收盘价成交 = 用信号价成交）。"""

    @pytest.mark.parametrize("name", DAILY_STRATEGIES)
    def test_orders_only_in_open_auction(self, name):
        tree = ast.parse((ROOT / "strategies" / f"{name}.py").read_text(encoding="utf-8"))
        funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert "open_auction" in funcs, f"{name} 缺少 open_auction T+1 执行入口"

        def order_calls(fn):
            return [c for c in ast.walk(fn)
                    if isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Name) and c.func.id in ORDER_APIS]

        for fname, fn in funcs.items():
            if fname == "open_auction":
                continue
            assert not order_calls(fn), \
                f"{name}.{fname} 仍在 BAR 阶段直接下单，违反 T+1 口径"
