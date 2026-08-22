# -*- coding: utf-8 -*-
"""海龟突破（A股蓝筹改良版）· 趋势跟踪

来源：GitHub ling-0729/KHunter turtle_strategy（A股实战改良），经典唐奇安通道突破
股票池：10 只高流动性蓝筹（与 momentum_rotation 同池）
入场（需同时满足）：
  1. 收盘价突破前 20 日最高价（唐奇安上线，不含当日）
  2. 当日阳线且上涨，上影线 < 4%（过滤假突破/冲高回落，适配 A股 T+1）
  3. 收盘价在 20 日均线上方（趋势过滤）
  4. 沪深300 在 120 日均线上方（大盘风控）
出场（任一触发）：
  1. 收盘跌破前 10 日最低价（唐奇安下线）
  2. 收盘跌破 入场价 - 2×ATR(20)（波动止损）
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
    context.n_entry = int(getattr(context, "n_entry", 20))   # 入场通道：N日高点
    context.n_exit = int(getattr(context, "n_exit", 10))     # 出场通道：N日低点
    context.atr_period = 20
    context.exit_atr = float(getattr(context, "exit_atr", 2.0))  # ATR 止损倍数
    context.hold_num = int(getattr(context, "hold_num", 3))
    context.benchmark_index = "000300.XSHG"
    context.ma_days = 120
    context.entry_price = {}    # 记录入场价用于 ATR 止损


def market_ok(context):
    hist = history_bars(context.benchmark_index, context.ma_days, "1d", "close")
    if hist is None or len(hist) < context.ma_days:
        return False
    return hist[-1] > hist.mean()


def atr(context, stock):
    hist = history_bars(stock, context.atr_period + 1, "1d")
    if hist is None or len(hist) < context.atr_period + 1:
        return None
    trs = []
    for i in range(1, len(hist)):
        h, l, pc = hist["high"][i], hist["low"][i], hist["close"][i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return float(np.mean(trs))


def channel_up(context, stock):
    """前 n_entry 日最高价（不含当日）"""
    hist = history_bars(stock, context.n_entry + 1, "1d", "high")
    if hist is None or len(hist) < context.n_entry + 1:
        return None
    return float(np.max(hist[:-1]))


def channel_down(context, stock):
    hist = history_bars(stock, context.n_exit + 1, "1d", "low")
    if hist is None or len(hist) < context.n_exit + 1:
        return None
    return float(np.min(hist[:-1]))


def entry_signal(context, stock, bar):
    up = channel_up(context, stock)
    if up is None or bar.close <= up:
        return False
    # 阳线 + 上涨
    if bar.close <= bar.open:
        return False
    # 上影线 < 4%
    top = max(bar.open, bar.close)
    if (bar.high - top) / top > 0.04:
        return False
    # MA20 趋势过滤
    ma = history_bars(stock, 20, "1d", "close")
    if ma is None or len(ma) < 20 or bar.close <= ma.mean():
        return False
    return True


def exit_signal(context, stock, bar):
    pos = context.portfolio.positions.get(stock)
    if pos is None or pos.quantity == 0:
        return False
    down = channel_down(context, stock)
    if down is not None and bar.close < down:
        logger.info(f"{stock} 跌破{context.n_exit}日低点，离场")
        return True
    ep = context.entry_price.get(stock)
    a = atr(context, stock)
    if ep and a and bar.close < ep - context.exit_atr * a:
        logger.info(f"{stock} 触发 {context.exit_atr}xATR 止损，离场")
        return True
    return False


def handle_bar(context, bar_dict):
    # 1) 先处理离场
    for stock in list(context.portfolio.positions.keys()):
        if stock not in context.stocks:
            continue
        bar = bar_dict[stock]
        if bar.isnan:
            continue
        if exit_signal(context, stock, bar):
            order_target_percent(stock, 0)
            context.entry_price.pop(stock, None)

    # 2) 大盘风控：不允许开新仓
    if not market_ok(context):
        return

    # 3) 空仓时寻找入场
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
        bar = bar_dict[stock]
        if bar.isnan:
            continue
        if entry_signal(context, stock, bar):
            order_target_percent(stock, weight)
            context.entry_price[stock] = float(bar.close)
            logger.info(f"{stock} 突破{context.n_entry}日高点入场 @{bar.close:.2f}")
            slots -= 1
