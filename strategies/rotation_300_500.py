# -*- coding: utf-8 -*-
"""二八轮动（有空仓版）· 蓝筹风格轮动

来源：GitHub wzhe06/SmartInvest（王喆《程序化交易实战》配套），A股经典风格轮动
标的：510300.XSHG 沪深300ETF（蓝筹腿）/ 510500.XSHG 中证500ETF（中小盘腿）
规则：
  1. 每日收盘后计算两只 ETF 的 20 日动量（区间涨幅）
  2. 动量更强且 > 0 的一方满仓；两者均 <= 0 时空仓持币（避险）
  3. T+1 收盘成交，由引擎处理
"""
from rqalpha.api import *


def init(context):
    context.etf_300 = "510300.XSHG"   # 沪深300ETF（蓝筹）
    context.etf_500 = "510500.XSHG"   # 中证500ETF
    context.n = int(getattr(context, "n", 20))  # 动量窗口（--extra-vars 可覆盖）


def momentum(stock, n):
    hist = history_bars(stock, n + 1, "1d", "close")
    if hist is None or len(hist) < n + 1 or hist[0] <= 0:
        return None
    return hist[-1] / hist[0] - 1.0


def handle_bar(context, bar_dict):
    m300 = momentum(context.etf_300, context.n)
    m500 = momentum(context.etf_500, context.n)
    if m300 is None or m500 is None:
        return

    if m300 >= m500 and m300 > 0:
        target = context.etf_300
    elif m500 > m300 and m500 > 0:
        target = context.etf_500
    else:
        target = None  # 双负空仓

    for s in (context.etf_300, context.etf_500):
        w = 0.98 if s == target else 0
        pos = context.portfolio.positions.get(s)
        cur = pos.market_value / context.portfolio.total_value if pos else 0
        if abs(cur - w) > 0.02:  # 偏离超 2% 才调仓，减少无效换手
            order_target_percent(s, w)
            logger.info(f"轮动: m300={m300:+.1%} m500={m500:+.1%} -> "
                        f"{'空仓' if target is None else target}")
