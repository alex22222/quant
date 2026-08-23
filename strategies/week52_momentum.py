# -*- coding: utf-8 -*-
"""52周高点动量轮动（蓝筹版）

来源：George & Hwang (2004) "The 52-Week High and Momentum Investing"，
      GitHub 量化社区广泛复现；锚定效应使「距52周高点越近」的股票后续动量越强
股票池：10 只高流动性蓝筹（与 momentum_rotation 同池）
规则：
  1. 每月第一个交易日调仓
  2. 得分 = 当前收盘 / 过去 252 日最高收盘（越接近 1 越强）
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
    context.lookback = 252      # 52 周 ≈ 252 交易日
    context.hold_num = 3
    context.benchmark_index = "000300.XSHG"
    context.ma_days = 120
    scheduler.run_monthly(rebalance, tradingday=1)


def market_ok(context):
    hist = history_bars(context.benchmark_index, context.ma_days, "1d", "close")
    if hist is None or len(hist) < context.ma_days:
        return False
    return hist[-1] > hist.mean()


def rebalance(context, bar_dict):
    if not market_ok(context):
        context.pending = ([], 0.0)  # 风控清仓推迟到 T+1 开盘（口径统一，策略库审查 P1）
        logger.info("风控触发：指数低于120日均线，将于次日开盘清仓持币")
        return

    scores = []
    for s in context.stocks:
        if is_suspended(s):
            continue
        hist = history_bars(s, context.lookback, "1d", "close")
        if hist is None or len(hist) < context.lookback:
            continue
        hi = float(np.max(hist))
        if hi > 0:
            scores.append((s, hist[-1] / hi))
    scores.sort(key=lambda x: x[1], reverse=True)
    targets = [s for s, _ in scores[:context.hold_num]]

    logger.info("52周高点得分: " + ", ".join(f"{s}({v:.2f})" for s, v in scores[:5]))

    # ⚠️ 口径统一：T 日信号，T+1 开盘集合竞价成交；这里只登记目标
    weight = 0.98 / len(targets) if targets else 0.0
    context.pending = (targets, weight)


def open_auction(context, bar_dict):
    """T+1 开盘集合竞价统一执行上一信号日登记的挂单。"""
    pending = getattr(context, "pending", None)
    if not pending:
        return
    context.pending = None
    targets, weight = pending
    for pos in get_positions():
        if pos.order_book_id not in targets:
            order_target_percent(pos.order_book_id, 0)
    for s in targets:
        order_target_percent(s, weight)
