# -*- coding: utf-8 -*-
"""双均线趋势跟随（蓝筹多标的版）

来源：经典 MA 金叉死叉体系（GitHub A股双均线实现的蓝筹增强版，替代已退役的 demo_dual_ma 单标的版）
股票池：10 只高流动性蓝筹（与 momentum_rotation 同池）
规则：
  1. 个股 20 日均线上穿 60 日均线（金叉）→ 建仓
  2. 20 日均线下穿 60 日均线（死叉）→ 清仓该标的
  3. 多标的等权，最多持有 3 只，先到先占
  4. 风控：沪深300 收于 120 日均线下方时清仓全部且不开新仓
"""
from rqalpha.api import *


POOL = [
    "600519.XSHG", "000858.XSHE", "600036.XSHG", "601318.XSHG", "300750.XSHE",
    "002594.XSHE", "601899.XSHG", "000333.XSHE", "600900.XSHG", "601012.XSHG",
]


def init(context):
    context.stocks = POOL
    context.fast = 20
    context.slow = 60
    context.hold_num = 3
    context.benchmark_index = "000300.XSHG"
    context.ma_days = 120
    context.prev_diff = {}   # 上一交易日 fast-slow 差值，用于判定穿越


def market_ok(context):
    hist = history_bars(context.benchmark_index, context.ma_days, "1d", "close")
    if hist is None or len(hist) < context.ma_days:
        return False
    return hist[-1] > hist.mean()


def ma_diff(context, stock):
    """当日 fast-slow 差值；数据不足返回 None"""
    hist = history_bars(stock, context.slow, "1d", "close")
    if hist is None or len(hist) < context.slow:
        return None
    return float(hist[-context.fast:].mean() - hist.mean())


def handle_bar(context, bar_dict):
    bull = market_ok(context)
    held = [s for s in context.portfolio.positions.keys() if s in context.stocks]

    # 1) 计算当日差值并读取昨日差值
    today, yesterday = {}, {}
    for stock in context.stocks:
        d = ma_diff(context, stock)
        if d is None:
            continue
        today[stock] = d
        yesterday[stock] = context.prev_diff.get(stock)

    # ⚠️ 口径统一（策略库审查 P1）：T 日信号，T+1 开盘集合竞价成交；
    # handle_bar 只登记买卖名单，open_auction 统一执行
    sells, buys = [], []

    # 2) 离场：死叉 或 大盘破位
    for stock in list(held):
        prev, cur = yesterday.get(stock), today.get(stock)
        dead_cross = prev is not None and cur is not None and prev > 0 >= cur
        if dead_cross or not bull:
            sells.append(stock)
            held.remove(stock)
            logger.info(f"{stock} {'大盘风控' if not bull else '死叉'}离场（次日开盘执行）")

    # 3) 入场：金叉且大盘向上，先到先占
    if bull:
        slots = context.hold_num - len(held)
        for stock in context.stocks:
            if slots <= 0:
                break
            if stock in held or is_suspended(stock):
                continue
            prev, cur = yesterday.get(stock), today.get(stock)
            if prev is not None and cur is not None and prev <= 0 < cur:
                buys.append(stock)
                held.append(stock)
                logger.info(f"{stock} 金叉建仓（次日开盘执行）")
                slots -= 1

    context.pending_sells = sells
    context.pending_buys = buys

    # 4) 每日滚动保存差值快照
    context.prev_diff = today


def open_auction(context, bar_dict):
    """T+1 开盘集合竞价统一执行 handle_bar 登记的挂单。"""
    sells = getattr(context, "pending_sells", None)
    buys = getattr(context, "pending_buys", None)
    if not sells and not buys:
        return
    context.pending_sells, context.pending_buys = [], []
    for stock in sells or []:
        order_target_percent(stock, 0)
    weight = 0.96 / context.hold_num
    for stock in buys or []:
        order_target_percent(stock, weight)
