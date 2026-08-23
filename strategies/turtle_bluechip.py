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
    "ma_days": 60,           # 大盘风控均线（季线；MA120 在 2024-09 政策急涨中重返市场太慢）
    "weight_cap": 0.96,      # 总仓位上限
    # 门禁 v2 攻关（2026-08-23）：以下两项默认关闭，保持与原行为一致
    "ma_slope_days": 0,      # >0 时要求 MA60 本身上行（牛市斜率过滤，防熊市反弹假信号）
    "chandelier_atr": 0.0,   # >0 时启用吊灯止盈：收盘 < 20日最高收盘 - N×ATR 即离场
    "chandelier_days": 20,   # 吊灯锚定窗口
    # 权益曲线滤波（E18 验证通过）：净值 < 其 120 日均线且市场非强牛时，仓位上限减半
    "equity_filter_days": 120,
    "equity_filter_scale": 0.5,
    "bull_band": 0.02,       # 指数高于 MA60 超 2%（强牛）时权益滤波不触发
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
                    params: dict, held: dict, nav_hist: list | None = None) -> SignalResult:
    """bars_map: {code: {"open","high","low","close": np.ndarray 旧→新}}；
    held: {code: 持仓成本}（当前持仓，出场判断依赖成本锚）；
    nav_hist: 策略自身净值序列（旧→新，权益曲线滤波用，可为 None）。

    返回目标持仓列表（留存持仓 + 新入场），detail 记录出入场原因与有效仓位上限。
    """
    n_exit, atr_n = params["n_exit"], params["atr_period"]
    n_entry, hold_num = params["n_entry"], params["hold_num"]
    exit_atr = params["exit_atr"]
    ma_days = params["ma_days"]

    market_ok = bool(
        index_close is not None and len(index_close) >= ma_days
        and index_close[-1] > index_close[-ma_days:].mean()
    )
    slope_days = params.get("ma_slope_days", 0)
    if market_ok and slope_days > 0:
        # 牛市斜率过滤：MA120 本身必须上行（当前均值 > slope_days 前的均值）
        if len(index_close) < ma_days + slope_days:
            market_ok = False
        else:
            ma_now = index_close[-ma_days:].mean()
            ma_then = index_close[-ma_days - slope_days:-slope_days].mean()
            market_ok = bool(ma_now > ma_then)

    targets, exits, entries = [], [], []

    # 1) 离场：跌破前 n_exit 日最低价 或 成本 - exit_atr×ATR 或吊灯止盈
    need_exit = max(n_exit, atr_n, params.get("chandelier_days", 20)) + 2
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
            ch_mult = params.get("chandelier_atr", 0.0)
            if ch_mult > 0:
                ch_days = params.get("chandelier_days", 20)
                anchor = float(np.max(bars["close"][-ch_days:]))
                ch_stop = anchor - ch_mult * _atr(bars, atr_n)
                if close < ch_stop:
                    exits.append({"code": code, "reason":
                                  f"吊灯止盈({ch_mult}xATR@{ch_stop:.2f})"})
                    continue
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

    # 强牛判定：指数高于 MA120 超过 bull_band 时，权益滤波不触发（牛市不自我降杠杆）
    market_weak = True
    bull_band = params.get("bull_band", 0.0)
    if bull_band > 0 and index_close is not None and len(index_close) >= ma_days:
        ma = float(index_close[-ma_days:].mean())
        market_weak = bool(index_close[-1] < ma * (1 + bull_band))

    return SignalResult(targets=targets, market_ok=market_ok,
                        detail={"entries": entries, "exits": exits,
                                "weight_cap": _effective_cap(params, nav_hist, market_weak)})


def _effective_cap(params: dict, nav_hist: list | None, market_weak: bool = True) -> float:
    """权益曲线滤波：净值低于其 N 日均线且市场不强时收缩仓位上限。"""
    cap = params["weight_cap"]
    if not market_weak:
        return cap
    efd = params.get("equity_filter_days", 0)
    if efd > 0 and nav_hist and len(nav_hist) >= efd:
        if nav_hist[-1] < float(np.mean(nav_hist[-efd:])):
            cap = cap * params.get("equity_filter_scale", 0.5)
    return cap


def generate_targets(data, params=None, positions=None, nav_hist=None) -> SignalResult:
    """统一信号接口（strategies/base.py 约定）。data: StrategyData。
    positions: {code: {"qty":..., "cost":...}} 当前持仓（日线策略依赖成本锚）。
    nav_hist: 策略净值序列（权益曲线滤波用，Paper 侧从 account 表读取）。"""
    p = dict(PARAMS)
    if params:
        p.update(params)
    held = {c: pos["cost"] for c, pos in (positions or {}).items() if pos.get("qty", 0) > 0}
    need = max(p["n_entry"], p["atr_period"], p.get("chandelier_days", 20), 20) + 2
    bars_map = {}
    for code in set(p["universe"]) | set(held):
        if data.is_suspended(code):
            continue
        bars_map[code] = data.ohlc(code, need)
    index_close = data.index_closes(p["benchmark"],
                                    p["ma_days"] + p.get("ma_slope_days", 0))
    return compute_targets(bars_map, index_close, p, held, nav_hist=nav_hist)


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
    from rqalpha.api import history_bars, is_suspended, logger

    p = context.p
    need = max(p["n_entry"], p["atr_period"], p.get("chandelier_days", 20), 20) + 2

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

    ib = history_bars(p["benchmark"], p["ma_days"] + p.get("ma_slope_days", 0),
                      "1d", "close")
    index_close = np.asarray(ib, dtype=float) if ib is not None else None

    # 权益曲线历史（滤波用）
    if not hasattr(context, "nav_hist"):
        context.nav_hist = []
    context.nav_hist.append(float(context.portfolio.unit_net_value))

    sig = compute_targets(bars_map, index_close, p, held,
                          nav_hist=context.nav_hist)

    for e in sig.detail["exits"]:
        logger.info(f"{e['code']} {e['reason']}，离场")
    for e in sig.detail["entries"]:
        logger.info(f"{e['code']} {e['reason']} @{e['close']:.2f}，入场")

    # ⚠️ 口径统一（策略库审查 P1）：T 日收盘出信号，T+1 开盘集合竞价成交，
    # 与 Paper（stage_paper）收益口径一致；禁止在 handle_bar 直接下单
    # （日线模式下会以当日收盘价成交，等于用产生信号的价格成交，高估收益）。
    weight = sig.detail.get("weight_cap", p["weight_cap"]) / p["hold_num"]
    context.pending_targets = (list(sig.targets), weight)


def open_auction(context, bar_dict):
    """T+1 开盘集合竞价统一执行 handle_bar 登记的挂单（实测成交价=次日开盘价）。"""
    pending = getattr(context, "pending_targets", None)
    if not pending:
        return
    context.pending_targets = None
    targets, weight = pending
    from rqalpha.api import order_target_percent
    held = [c for c, pos in context.portfolio.positions.items() if pos.quantity > 0]
    # 卖出调出名单的持仓
    for code in held:
        if code not in targets:
            order_target_percent(code, 0)
    # 等权对齐目标持仓（含已有持仓：权益滤波收缩时同步减仓）
    for code in targets:
        order_target_percent(code, weight)
