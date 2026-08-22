# -*- coding: utf-8 -*-
"""多股票动量轮动策略

股票池：10 只高流动性大盘股（覆盖消费/金融/新能源/公用事业）
规则：
  1. 每月第一个交易日调仓
  2. 计算每只股票过去 20 个交易日的动量（区间涨幅）
  3. 等权买入动量最强的 3 只
  4. 风控：沪深300 收于 120 日均线下方时，全部清仓持币（避险）

改进计划 Phase 1：信号核心抽出为纯函数 compute_targets()，
rqalpha 回测（init/rebalance）与 Paper（generate_targets）共用同一逻辑。
"""
import os
import sys

import numpy as np

# rqalpha 编译策略时会改写 __file__，统一从环境变量定位项目根
_ROOT = os.environ.get("QUANT_ROOT", str(__import__("pathlib").Path.cwd()))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from strategies.base import SignalResult

FAMILY = "momentum_rotation"

PARAMS = {
    "universe": [
        "600519.XSHG",  # 贵州茅台
        "000858.XSHE",  # 五粮液
        "600036.XSHG",  # 招商银行
        "601318.XSHG",  # 中国平安
        "300750.XSHE",  # 宁德时代
        "002594.XSHE",  # 比亚迪
        "601899.XSHG",  # 紫金矿业
        "000333.XSHE",  # 美的集团
        "600900.XSHG",  # 长江电力
        "601012.XSHG",  # 隆基绿能
    ],
    "momentum_days": 20,     # 动量窗口
    "hold_num": 3,           # 持仓数量
    "benchmark": "000300.XSHG",
    "ma_days": 120,          # 风控均线
}


# ─────────────────────────────────────────────────────────────
# 纯信号核心（无任何交易引擎依赖）：回测与 Paper 共用
# ─────────────────────────────────────────────────────────────

def compute_targets(closes_map: dict, index_close: np.ndarray, params: dict) -> SignalResult:
    """closes_map: {code: np.ndarray(收盘价序列, 旧→新)}，index_close: 指数收盘价序列。

    返回目标持仓列表与风控状态。数据不足的股票直接跳过。
    """
    mom_days = params["momentum_days"]
    ma_days = params["ma_days"]
    hold_num = params["hold_num"]

    market_ok = bool(
        index_close is not None and len(index_close) >= ma_days
        and index_close[-1] > index_close[-ma_days:].mean()
    )

    table = []
    for code, c in closes_map.items():
        if c is None or len(c) < mom_days + 1 or c[-mom_days - 1] <= 0:
            continue
        table.append((code, float(c[-1] / c[-mom_days - 1] - 1.0), float(c[-1])))
    table.sort(key=lambda x: -x[1])

    targets = [s for s, _, _ in table[:hold_num]] if market_ok else []
    return SignalResult(
        targets=targets,
        market_ok=market_ok,
        detail={"momentum_table": [
            {"code": s, "mom": round(m, 4), "close": p} for s, m, p in table
        ]},
    )


def generate_targets(data, params=None, positions=None) -> SignalResult:
    """统一信号接口（strategies/base.py 约定）。data: StrategyData。"""
    p = dict(PARAMS)
    if params:
        p.update(params)
    closes_map = {}
    for code in p["universe"]:
        if data.is_suspended(code):
            continue
        closes_map[code] = data.closes(code, p["momentum_days"] + 1)
    index_close = data.index_closes(p["benchmark"], p["ma_days"])
    return compute_targets(closes_map, index_close, p)


# ─────────────────────────────────────────────────────────────
# rqalpha 回测入口（信号同样走 compute_targets，禁止另写一份逻辑）
# ─────────────────────────────────────────────────────────────

def init(context):
    from rqalpha.api import scheduler
    context.params = dict(PARAMS)
    # --extra-vars 覆盖（门禁扰动测试用），universe 不支持覆盖
    for k in PARAMS:
        if k != "universe" and hasattr(context, k):
            context.params[k] = getattr(context, k)
    context.stocks = context.params["universe"]
    scheduler.run_monthly(rebalance, tradingday=1)


class _RQData:
    """rqalpha history_bars → 收盘价序列适配（仅 compute_targets 内部使用）。"""

    def __init__(self):
        from rqalpha.api import history_bars, is_suspended
        self._hist = history_bars
        self._susp = is_suspended

    def closes_map(self, universe, n):
        out = {}
        for s in universe:
            if self._susp(s):
                continue
            h = self._hist(s, n, "1d", "close")
            out[s] = np.asarray(h, dtype=float) if h is not None and len(h) >= n else None
        return out

    def index_close(self, code, n):
        h = self._hist(code, n, "1d", "close")
        return np.asarray(h, dtype=float) if h is not None and len(h) >= n else None


def rebalance(context, bar_dict):
    from rqalpha.api import get_positions, order_target_percent, logger
    p = context.params
    d = _RQData()
    result = compute_targets(
        d.closes_map(p["universe"], p["momentum_days"] + 1),
        d.index_close(p["benchmark"], p["ma_days"]),
        p,
    )

    if not result.market_ok:
        for pos in get_positions():
            order_target_percent(pos.order_book_id, 0)
        logger.info("风控触发：指数低于120日均线，清仓持币")
        return

    targets = result.targets
    logger.info("动量排名: " + ", ".join(
        f"{t['code']}({t['mom']:+.1%})" for t in result.detail["momentum_table"][:5]))

    # 卖出跌出名单的持仓
    for pos in get_positions():
        if pos.order_book_id not in targets:
            order_target_percent(pos.order_book_id, 0)

    if targets:
        weight = 0.98 / len(targets)
        for s in targets:
            order_target_percent(s, weight)


def handle_bar(context, bar_dict):
    pass
