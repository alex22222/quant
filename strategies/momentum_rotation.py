# -*- coding: utf-8 -*-
"""多股票动量轮动策略

股票池：10 只高流动性大盘股（覆盖消费/金融/新能源/公用事业）
规则：
  1. 每月第一个交易日调仓
  2. 计算每只股票过去 20 个交易日的动量（区间涨幅）
  3. 等权买入动量最强的 3 只
  4. 风控：沪深300 收于 120 日均线下方时，全部清仓持币（避险）
"""
from rqalpha.api import *


def init(context):
    context.stocks = [
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
    ]
    context.momentum_days = 20    # 动量窗口
    context.hold_num = 3          # 持仓数量
    context.benchmark_index = "000300.XSHG"
    context.ma_days = 120         # 风控均线

    # 每月第 1 个交易日执行调仓
    scheduler.run_monthly(rebalance, tradingday=1)


def momentum(context, stock):
    hist = history_bars(stock, context.momentum_days + 1, "1d", "close")
    if hist is None or len(hist) < context.momentum_days + 1:
        return None
    if hist[0] <= 0:
        return None
    return hist[-1] / hist[0] - 1.0


def market_ok(context):
    """风控：指数在 120 日均线上方才允许持仓"""
    hist = history_bars(context.benchmark_index, context.ma_days, "1d", "close")
    if hist is None or len(hist) < context.ma_days:
        return False
    return hist[-1] > hist.mean()


def rebalance(context, bar_dict):
    # 1) 风控过滤：大盘破位则清仓
    if not market_ok(context):
        for pos in get_positions():
            order_target_percent(pos.order_book_id, 0)
        logger.info("风控触发：指数低于120日均线，清仓持币")
        return

    # 2) 计算动量并排序
    scores = []
    for s in context.stocks:
        if is_suspended(s):
            continue
        m = momentum(context, s)
        if m is not None:
            scores.append((s, m))
    scores.sort(key=lambda x: x[1], reverse=True)
    targets = [s for s, _ in scores[:context.hold_num]]

    logger.info("动量排名: " + ", ".join(
        f"{s}({m:+.1%})" for s, m in scores[:5]))

    # 3) 卖出跌出名单的持仓
    for pos in get_positions():
        if pos.order_book_id not in targets:
            order_target_percent(pos.order_book_id, 0)

    # 4) 等权买入目标持仓
    weight = 0.98 / len(targets)
    for s in targets:
        order_target_percent(s, weight)


def handle_bar(context, bar_dict):
    pass
