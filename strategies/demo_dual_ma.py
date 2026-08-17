# -*- coding: utf-8 -*-
"""双均线示例策略：验证 RQAlpha 全链路
标的：平安银行 000001.XSHE
规则：5 日均线上穿 20 日均线买入，下穿卖出（A股 T+1 由引擎自动处理）
"""
from rqalpha.api import *


def init(context):
    context.stock = "000001.XSHE"
    context.short = 5
    context.long = 20


def handle_bar(context, bar_dict):
    hist = history_bars(context.stock, context.long + 1, "1d", "close")
    if len(hist) < context.long + 1:
        return
    short_now = hist[-context.short:].mean()
    long_now = hist[-context.long:].mean()
    short_prev = hist[-context.short - 1:-1].mean()
    long_prev = hist[-context.long - 1:-1].mean()

    pos = context.portfolio.positions[context.stock].quantity
    # 金叉：满仓买入；死叉：清仓
    if short_prev <= long_prev and short_now > long_now and pos == 0:
        order_percent(context.stock, 0.95)
    elif short_prev >= long_prev and short_now < long_now and pos > 0:
        order_target_percent(context.stock, 0)
