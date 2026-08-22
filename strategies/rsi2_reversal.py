# -*- coding: utf-8 -*-
"""RSI-2 超跌反弹（蓝筹均值回归）

来源：Larry Connors RSI-2 经典均值回归体系（GitHub 多个 A股实现复用，如 TushareDB oversold_rebound）
逻辑：大盘股长期向上、短期超跌后回归均值，是 A股蓝筹中胜率最高的短线形态之一
股票池：10 只高流动性蓝筹（与 momentum_rotation 同池）
入场（需同时满足）：
  1. 沪深300 在 200 日均线上方（只在大趋势向上时做反弹）
  2. 个股收盘价在自身 200 日均线上方（只买上升趋势中的回调）
  3. RSI(2) < 10（短线严重超跌）
出场：RSI(2) > 70 或 收盘价上穿 5 日均线（均值回归完成）
仓位：最多持有 3 只，等权
"""
from rqalpha.api import *
import numpy as np


POOL = [
    "600519.XSHG", "000858.XSHE", "600036.XSHG", "601318.XSHG", "300750.XSHE",
    "002594.XSHE", "601899.XSHG", "000333.XSHE", "600900.XSHG", "601012.XSHG",
]


def init(context):
    context.stocks = POOL
    context.rsi_period = 2
    context.buy_threshold = 10    # RSI2 超跌线
    context.sell_threshold = 70   # RSI2 超买回撤线
    context.trend_days = 200      # 趋势过滤均线
    context.exit_ma = 5           # 均值回归出场均线
    context.hold_num = 3
    context.benchmark_index = "000300.XSHG"


def rsi(closes, period):
    if closes is None or len(closes) < period + 1:
        return None
    diff = np.diff(closes)
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    avg_gain = gains[-period:].mean()
    avg_loss = losses[-period:].mean()
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def handle_bar(context, bar_dict):
    # 大盘趋势过滤
    bench = history_bars(context.benchmark_index, context.trend_days, "1d", "close")
    bull = bench is not None and len(bench) >= context.trend_days and bench[-1] > bench.mean()

    held = [s for s in context.portfolio.positions.keys() if s in context.stocks]

    # 1) 离场：RSI2 > 70 或收盘上穿 MA5
    for stock in list(held):
        closes = history_bars(stock, context.exit_ma + context.rsi_period + 2, "1d", "close")
        if closes is None or len(closes) < context.exit_ma + 2:
            continue
        r = rsi(closes, context.rsi_period)
        ma5 = closes[-context.exit_ma:].mean()
        if (r is not None and r > context.sell_threshold) or closes[-1] > ma5:
            order_target_percent(stock, 0)
            logger.info(f"{stock} 均值回归完成离场 (RSI2={r:.0f})")

    if not bull:
        return

    # 2) 入场
    held = [s for s in context.portfolio.positions.keys() if s in context.stocks]
    slots = context.hold_num - len(held)
    if slots <= 0:
        return
    weight = 0.96 / context.hold_num
    for stock in context.stocks:
        if slots <= 0:
            break
        if stock in held or is_suspended(stock):
            continue
        closes = history_bars(stock, context.trend_days, "1d", "close")
        if closes is None or len(closes) < context.trend_days:
            continue
        if closes[-1] <= closes.mean():  # 自身处于上升趋势
            continue
        r = rsi(closes, context.rsi_period)
        if r is not None and r < context.buy_threshold:
            order_target_percent(stock, weight)
            logger.info(f"{stock} RSI2={r:.1f} 超跌买入 @{closes[-1]:.2f}")
            slots -= 1
