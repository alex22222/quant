# -*- coding: utf-8 -*-
"""夏普动量轮动（风险调整动量，蓝筹版）

来源：风险调整动量（Risk-adjusted Momentum）体系，GitHub 多因子社区常用复合因子
      （动量收益 / 波动率），较纯动量在 A股蓝筹上回撤更小、胜率更高
股票池：10 只高流动性蓝筹（与 momentum_rotation 同池）
规则：
  1. 每月第一个交易日调仓
  2. 得分 = 60 日区间涨幅 / 60 日日收益率标准差（夏普式动量）
  3. 等权买入得分最高的 3 只
  4. 风控：沪深300 收于 120 日均线下方时清仓持币
"""
from rqalpha.api import *
import numpy as np


POOL = [
    "600519.XSHG", "000858.XSHE", "600036.XSHG", "601318.XSHG", "300750.XSHE",
    "002594.XSHE", "601899.XSHG", "000333.XSHE", "600900.XSHG", "601012.XSHG",
]


def init(context):
    context.stocks = POOL
    context.lookback = 60
    context.hold_num = 3
    context.benchmark_index = "000300.XSHG"
    context.ma_days = 120
    scheduler.run_monthly(rebalance, tradingday=1)


def market_ok(context):
    hist = history_bars(context.benchmark_index, context.ma_days, "1d", "close")
    if hist is None or len(hist) < context.ma_days:
        return False
    return hist[-1] > hist.mean()


def sharpe_momentum(context, stock):
    hist = history_bars(stock, context.lookback + 1, "1d", "close")
    if hist is None or len(hist) < context.lookback + 1 or hist[0] <= 0:
        return None
    ret_total = hist[-1] / hist[0] - 1.0
    daily = np.diff(hist) / hist[:-1]
    vol = float(np.std(daily))
    if vol <= 1e-8:
        return None
    return ret_total / vol


def rebalance(context, bar_dict):
    if not market_ok(context):
        for pos in get_positions():
            order_target_percent(pos.order_book_id, 0)
        logger.info("风控触发：指数低于120日均线，清仓持币")
        return

    scores = []
    for s in context.stocks:
        if is_suspended(s):
            continue
        v = sharpe_momentum(context, s)
        if v is not None:
            scores.append((s, v))
    scores.sort(key=lambda x: x[1], reverse=True)
    targets = [s for s, _ in scores[:context.hold_num]]

    logger.info("夏普动量排名: " + ", ".join(f"{s}({v:.2f})" for s, v in scores[:5]))

    for pos in get_positions():
        if pos.order_book_id not in targets:
            order_target_percent(pos.order_book_id, 0)
    if targets:
        weight = 0.98 / len(targets)
        for s in targets:
            order_target_percent(s, weight)
