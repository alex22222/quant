# -*- coding: utf-8 -*-
"""研究草案（momentum）：Regime-Based Sector Rotation Beats the Index at Full Deploym

来源：quant-x-monitor 2026-08-23 · Quantocracy · https://x.com/Quantocracy/status/2091303368937681407
分类：trading_signal（关键词命中：轮动）

⚠️ 本文件由 pipeline/research_ingest.py 自动生成的**草案骨架**：
   信号核心为模板默认实现，参数未经核实。晋级 candidate 前必须人工核实
   逻辑与参数，并走 pipeline.promote --differentiation 说明与在库策略的
   区分度（AGENT.md 工作纪律第 7 条）。
"""
import os
import sys

import numpy as np

_ROOT = os.environ.get("QUANT_ROOT", str(__import__("pathlib").Path.cwd()))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from strategies.base import SignalResult

FAMILY = "ingest_momentum"

PARAMS = {
    "universe": [
        "600519.XSHG",
        "000858.XSHE",
        "600036.XSHG",
        "601318.XSHG",
        "300750.XSHE",
        "002594.XSHE",
        "601899.XSHG",
        "000333.XSHE",
        "600900.XSHG",
        "601012.XSHG"
],
    "momentum_days": 20,
    "hold_num": 3,
    "benchmark": "000300.XSHG",
    "ma_days": 120,
    # TODO(人工)：按来源线索核实/补充参数与信号逻辑
}


def compute_targets(closes_map, index_close, params):
    """momentum 模板默认实现（草案，待人工核实）。"""
    ma_days = params["ma_days"]
    market_ok = bool(
        index_close is not None and len(index_close) >= ma_days
        and index_close[-1] > index_close[-ma_days:].mean()
    )
    mom_days = params["momentum_days"]
    table = []
    for code, c in closes_map.items():
        if c is None or len(c) < mom_days + 1 or c[-mom_days - 1] <= 0:
            continue
        table.append((code, float(c[-1] / c[-mom_days - 1] - 1.0)))
    table.sort(key=lambda x: -x[1])
    targets = [s for s, _ in table[: params["hold_num"]]] if market_ok else []
    return SignalResult(targets=targets, market_ok=market_ok,
                        detail={"draft": True, "template": "momentum"})


def generate_targets(data, params=None, positions=None):
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


# ── rqalpha 回测入口（与 compute_targets 同一信号核心）──

def init(context):
    from rqalpha.api import scheduler
    context.params = dict(PARAMS)
    context.stocks = context.params["universe"]
    scheduler.run_monthly(rebalance, tradingday=1)


def rebalance(context, bar_dict):
    from rqalpha.api import history_bars, is_suspended, logger
    p = context.params
    closes_map = {}
    for s in p["universe"]:
        if is_suspended(s):
            continue
        h = history_bars(s, p["momentum_days"] + 1, "1d", "close")
        closes_map[s] = (np.asarray(h, dtype=float)
                         if h is not None and len(h) >= p["momentum_days"] + 1 else None)
    idx = history_bars(p["benchmark"], p["ma_days"], "1d", "close")
    idx = np.asarray(idx, dtype=float) if idx is not None and len(idx) >= p["ma_days"] else None
    result = compute_targets(closes_map, idx, p)
    weight = 0.96 / len(result.targets) if result.targets else 0.0
    context.pending = (list(result.targets), weight)
    logger.info(f"草案信号: {result.targets} market_ok={result.market_ok}")


def open_auction(context, bar_dict):
    pending = getattr(context, "pending", None)
    if not pending:
        return
    context.pending = None
    targets, weight = pending
    from rqalpha.api import get_positions, order_target_percent
    for pos in get_positions():
        if pos.order_book_id not in targets:
            order_target_percent(pos.order_book_id, 0)
    for s in targets:
        order_target_percent(s, weight)


def handle_bar(context, bar_dict):
    pass
