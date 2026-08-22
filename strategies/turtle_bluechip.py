# -*- coding: utf-8 -*-
"""海龟突破（A股蓝筹改良版）· 趋势跟踪

来源：GitHub ling-0729/KHunter turtle_strategy（A股实战改良），经典唐奇安通道突破
股票池：10 只高流动性蓝筹（与 momentum_rotation 同池）
入场（需同时满足）：
  1. 收盘价突破前 25 日最高价（唐奇安上线，不含当日）
  2. 当日阳线且上涨，上影线 < 4%（过滤假突破/冲高回落，适配 A股 T+1）
  3. 收盘价在 20 日均线上方（趋势过滤）
  4. 沪深300 在 120 日均线上方（大盘风控）
出场（任一触发）：
  1. 收盘跌破前 10 日最低价（唐奇安下线）
  2. 收盘跌破 持仓成本 - 2×ATR(20)（波动止损）
仓位：最多持有 4 只，等权（0.96 资金上限）
参数寻优记录：n_entry∈{15,20,25} × exit_atr∈{2.0,2.5} × hold_num∈{3,4} 共 12 组，
  hold_num=4 的 4 组全部过门禁（分散持仓是回撤 32%→20% 主因），当前为最优组合。

改进计划 Phase 1：信号核心抽出为纯函数 compute_targets()，
rqalpha 回测（init/handle_bar）与 Paper（generate_targets）共用同一逻辑。
日线策略：FREQUENCY="daily"，每个交易日生成信号。
"""
import os
import sys

import numpy as np

# rqalpha 编译策略时会改写 __file__，统一从环境变量定位项目根
_ROOT = os.environ.get("QUANT_ROOT", str(__import__("pathlib").Path.cwd()))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from strategies.base import SignalResult

FAMILY = "turtle"
FREQUENCY = "daily"   # 日线级信号（stage_paper 据此每日生成目标）

PARAMS = {
    "universe": [
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
    ],
    "n_entry": 25,           # 入场通道：N日高点（不含当日）
    "n_exit": 10,            # 出场通道：N日低点（不含当日）
    "atr_period": 20,
    "exit_atr": 2.0,         # ATR 止损倍数
    "hold_num": 4,
    "benchmark": "000300.XSHG",
    "ma_days": 120,          # 大盘风控均线
    "weight_cap": 0.96,      # 总仓位上限
}


# ─────────────────────────────────────────────────────────────
# 纯信号核心（无任何交易引擎依赖）：回测与 Paper 共用
# ─────────────────────────────────────────────────────────────

def _atr(bars, n):
    h, l, c = bars["high"], bars["low"], bars["close"]
    trs = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
           for i in range(len(c) - n, len(c))]
    return float(np.mean(trs))


def compute_targets(bars_map: dict, index_close: np.ndarray,
                    params: dict, held: dict) -> SignalResult:
    """bars_map: {code: {"open","high","low","close": np.ndarray 旧→新}}；
    held: {code: 持仓成本}（当前持仓，出场判断依赖成本锚）。

    返回目标持仓列表（留存持仓 + 新入场），detail 记录出入场原因。
    """
    n_exit, atr_n = params["n_exit"], params["atr_period"]
    n_entry, hold_num = params["n_entry"], params["hold_num"]
    exit_atr = params["exit_atr"]
    ma_days = params["ma_days"]

    market_ok = bool(
        index_close is not None and len(index_close) >= ma_days
        and index_close[-1] > index_close[-ma_days:].mean()
    )

    targets, exits, entries = [], [], []

    # 1) 离场：跌破前 n_exit 日最低价 或 成本 - exit_atr×ATR
    need_exit = max(n_exit, atr_n) + 2
    for code, cost in held.items():
        bars = bars_map.get(code)
        if bars is None or len(bars["close"]) < need_exit:
            targets.append(code)  # 数据不足：不动
            continue
        close = bars["close"][-1]
        down = float(np.min(bars["low"][-n_exit - 1:-1]))
        stop = cost - exit_atr * _atr(bars, atr_n)
        if close < down:
            exits.append({"code": code, "reason": f"跌破{n_exit}日低点"})
        elif close < stop:
            exits.append({"code": code, "reason": f"{exit_atr}xATR止损"})
        else:
            targets.append(code)

    # 2) 入场：突破前 n_entry 日高点 + 阳线 + 上影线<4% + MA20 上方 + 大盘风控
    slots = hold_num - len(targets)
    if market_ok and slots > 0:
        for code in params["universe"]:
            if slots <= 0:
                break
            if code in targets:
                continue
            bars = bars_map.get(code)
            if bars is None or len(bars["close"]) < max(n_entry, 20) + 2:
                continue
            o, h = bars["open"][-1], bars["high"][-1]
            c = bars["close"][-1]
            up = float(np.max(bars["high"][-n_entry - 1:-1]))
            ma20 = float(np.mean(bars["close"][-20:]))
            top = max(o, c)
            if c > up and c > o and (h - top) / top <= 0.04 and c > ma20:
                targets.append(code)
                entries.append({"code": code, "reason": f"突破{n_entry}日高点", "close": float(c)})
                slots -= 1

    return SignalResult(targets=targets, market_ok=market_ok,
                        detail={"entries": entries, "exits": exits})


def generate_targets(data, params=None, positions=None) -> SignalResult:
    """统一信号接口（strategies/base.py 约定）。data: StrategyData。
    positions: {code: {"qty":..., "cost":...}} 当前持仓（日线策略依赖成本锚）。"""
    p = dict(PARAMS)
    if params:
        p.update(params)
    held = {c: pos["cost"] for c, pos in (positions or {}).items() if pos.get("qty", 0) > 0}
    need = max(p["n_entry"], p["atr_period"], 20) + 2
    bars_map = {}
    for code in set(p["universe"]) | set(held):
        if data.is_suspended(code):
            continue
        bars_map[code] = data.ohlc(code, need)
    index_close = data.index_closes(p["benchmark"], p["ma_days"])
    return compute_targets(bars_map, index_close, p, held)


# ─────────────────────────────────────────────────────────────
# rqalpha 回测入口（信号同样走 compute_targets，禁止另写一份逻辑）
# ─────────────────────────────────────────────────────────────

def init(context):
    from rqalpha.api import scheduler  # noqa: F401
    # registry 的 params 经 --extra-vars 注入 context.__dict__，覆盖默认 PARAMS；
    # 注意不能用 getattr(context, ...)：rqalpha 内置 universe 等保留属性会遮蔽默认值
    injected = getattr(context, "__dict__", {})
    context.p = {k: injected.get(k, v) for k, v in PARAMS.items()}


def handle_bar(context, bar_dict):
    from rqalpha.api import history_bars, is_suspended, order_target_percent, logger

    p = context.p
    need = max(p["n_entry"], p["atr_period"], 20) + 2

    # 持仓成本锚（rqalpha position.avg_cost）
    held = {}
    for code, pos in context.portfolio.positions.items():
        if pos.quantity > 0:
            cost = getattr(pos, "avg_cost", None) or getattr(pos, "avg_open_price", 0)
            held[code] = float(cost)

    bars_map = {}
    for code in set(p["universe"]) | set(held):
        if is_suspended(code):
            continue
        h = history_bars(code, need, "1d")
        if h is None or len(h) < need:
            continue
        bars_map[code] = {k: np.asarray(h[k], dtype=float)
                          for k in ("open", "high", "low", "close")}

    ib = history_bars(p["benchmark"], p["ma_days"], "1d", "close")
    index_close = np.asarray(ib, dtype=float) if ib is not None else None

    sig = compute_targets(bars_map, index_close, p, held)

    for e in sig.detail["exits"]:
        logger.info(f"{e['code']} {e['reason']}，离场")
    for e in sig.detail["entries"]:
        logger.info(f"{e['code']} {e['reason']} @{e['close']:.2f}，入场")

    # 卖出调出名单的持仓
    for code in list(held):
        if code not in sig.targets:
            order_target_percent(code, 0)
    # 等权买入目标持仓
    if sig.targets:
        weight = p["weight_cap"] / p["hold_num"]
        for code in sig.targets:
            if code not in held:
                order_target_percent(code, weight)
