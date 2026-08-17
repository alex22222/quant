# -*- coding: utf-8 -*-
"""多股票动量轮动 + 止损锚对照实验

用法（--extra-vars 注入 context.stop）：
  --extra-vars '{"stop":"none"}'   基线：无个股止损
  --extra-vars '{"stop":"atr"}'    ATR 吊灯止损：历史最高收盘 - 2.5*ATR(14)，只上不下
  --extra-vars '{"stop":"kde"}'    KDE 位止损：入场时锚定下方最近筹码共识位，每周上移，只上不下

三个变体共用同一套入场逻辑（动量轮动 + 120 日线风控），唯一差异是止损机制。
"""
import sys
sys.path.insert(0, "/Users/henry/projects/quant/kde_levels")

import numpy as np
import pandas as pd
from rqalpha.api import *
from levels import compute_levels

ATR_MULT = 2.5
LEVEL_MAX_DROP = 0.20   # 只接受入场价下方 20% 以内的支撑位


def init(context):
    context.stocks = [
        "600519.XSHG", "000858.XSHE", "600036.XSHG", "601318.XSHG",
        "300750.XSHE", "002594.XSHE", "601899.XSHG", "000333.XSHE",
        "600900.XSHG", "601012.XSHG",
    ]
    context.momentum_days = 20
    context.hold_num = 3
    context.benchmark_index = "000300.XSHG"
    context.ma_days = 120

    context.stops = {}        # order_book_id -> 当前止损价
    context.hwm = {}          # order_book_id -> 持仓期间最高收盘
    context.entry_px = {}     # order_book_id -> 入场参考价
    context.stopped_out = set()  # 本月内被止损踢出的票，下月调仓前不再买回

    scheduler.run_monthly(rebalance, tradingday=1)
    scheduler.run_weekly(update_stops, tradingday=5)  # 每周五上移止损


def _hist_df(context, stock, n):
    arr = history_bars(stock, n, "1d",
                       ["open", "close", "high", "low", "total_turnover"])
    if arr is None or len(arr) < n:
        return None
    return pd.DataFrame(arr)


def _atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean().iloc[-1]


def make_stop(context, stock, ref_price):
    """按模式生成止损价。kde 模式找不到合适支撑位时退化为 ATR 止损"""
    df = _hist_df(context, stock, 250)
    if df is None:
        return None
    a = _atr(df)
    if context.stop == "atr":
        return ref_price - ATR_MULT * a
    if context.stop in ("kde", "kde_profit"):
        levels, proms, _, _ = compute_levels(df)
        below = [lv for lv in levels if ref_price * (1 - LEVEL_MAX_DROP) <= lv < ref_price]
        if below:
            return max(below)          # 最近（最高）的下方支撑位
        return ref_price - ATR_MULT * a  # 兜底
    return None


def momentum(context, stock):
    hist = history_bars(stock, context.momentum_days + 1, "1d", "close")
    if hist is None or len(hist) < context.momentum_days + 1 or hist[0] <= 0:
        return None
    return hist[-1] / hist[0] - 1.0


def market_ok(context):
    hist = history_bars(context.benchmark_index, context.ma_days, "1d", "close")
    if hist is None or len(hist) < context.ma_days:
        return False
    return hist[-1] > hist.mean()


def rebalance(context, bar_dict):
    context.stopped_out = set()

    if not market_ok(context):
        for pos in get_positions():
            order_target_percent(pos.order_book_id, 0)
            _clear(context, pos.order_book_id)
        logger.info("风控触发：清仓持币")
        return

    scores = []
    for s in context.stocks:
        if is_suspended(s):
            continue
        m = momentum(context, s)
        if m is not None:
            scores.append((s, m))
    scores.sort(key=lambda x: x[1], reverse=True)
    targets = [s for s, _ in scores[:context.hold_num]]

    for pos in get_positions():
        if pos.order_book_id not in targets:
            order_target_percent(pos.order_book_id, 0)
            _clear(context, pos.order_book_id)

    weight = 0.98 / len(targets)
    for s in targets:
        order_target_percent(s, weight)
        context.entry_px.setdefault(s, bar_dict[s].close)
        # kde_profit 模式入场不挂止损，等浮盈激活；其余模式立即挂
        if context.stop in ("atr", "kde") and s not in context.stops:
            st = make_stop(context, s, bar_dict[s].close)
            if st:
                context.stops[s] = st
                context.hwm[s] = bar_dict[s].close
                logger.info(f"{s} 初始止损: {st:.2f} ({context.stop})")


PROFIT_ACTIVATE = 1.05   # 浮盈 5% 才激活 KDE 止损


def update_stops(context, bar_dict):
    """每周五：跟踪上移止损（只上不下）"""
    if context.stop == "none":
        return
    if context.stop == "kde_profit":
        _update_kde_profit(context, bar_dict)
        return
    for s in list(context.stops.keys()):
        close = bar_dict[s].close
        context.hwm[s] = max(context.hwm.get(s, close), close)
        if context.stop == "atr":
            df = _hist_df(context, stock=s, n=30)
            if df is None:
                continue
            new_stop = context.hwm[s] - ATR_MULT * _atr(df)
        else:  # kde：价格涨离旧锚后，重新锚定当前价下方最近支撑位
            new_stop = make_stop(context, s, close)
            if new_stop is None:
                continue
        if new_stop > context.stops[s]:
            logger.info(f"{s} 止损上移: {context.stops[s]:.2f} -> {new_stop:.2f}")
            context.stops[s] = new_stop


def _update_kde_profit(context, bar_dict):
    """盈利仓专属 KDE 止损：浮盈 >5% 才激活，止损不低于成本价（锁保本），只上不下"""
    for s, entry in list(context.entry_px.items()):
        pos = get_position(s)
        if pos is None or pos.quantity == 0 or s in context.stopped_out:
            continue
        close = bar_dict[s].close
        if close < entry * PROFIT_ACTIVATE:
            continue  # 浮盈不足，不挂止损（避开震荡 whipsaw）
        new_stop = make_stop(context, s, close)
        if new_stop is None:
            continue
        new_stop = max(new_stop, entry)  # 至少保本
        if s not in context.stops:
            context.stops[s] = new_stop
            logger.info(f"{s} 浮盈激活止损: {new_stop:.2f} (成本 {entry:.2f})")
        elif new_stop > context.stops[s]:
            logger.info(f"{s} 止损上移: {context.stops[s]:.2f} -> {new_stop:.2f}")
            context.stops[s] = new_stop


def handle_bar(context, bar_dict):
    if context.stop == "none":
        return
    for s in list(context.stops.keys()):
        pos = get_position(s)
        if pos is None or pos.quantity == 0:
            continue
        if s in context.stopped_out:
            continue
        if bar_dict[s].close < context.stops[s]:
            order_target_percent(s, 0)
            logger.info(f"{s} 触发止损 @{bar_dict[s].close:.2f} < {context.stops[s]:.2f}，离场")
            context.stopped_out.add(s)
            _clear(context, s)


def _clear(context, s):
    context.stops.pop(s, None)
    context.hwm.pop(s, None)
    context.entry_px.pop(s, None)
