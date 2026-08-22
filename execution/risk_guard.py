# -*- coding: utf-8 -*-
"""risk_guard：实盘下单前的最后门禁（Loop Engineering 风控线）

每条订单独立校验，任一硬规则不通过即拦截并记录原因。
日级熔断：当日权益较昨收权益回撤超 max_daily_loss_pct → 全停。
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .broker_base import Position, to_std
from .market import Quote
from .order_manager import OrderPlan, OrderSpec


@dataclass
class RiskConfig:
    max_weight_per_stock: float = 0.35   # 单票买入后市值 / 权益 上限
    max_total_weight: float = 0.98       # 总仓位上限
    max_orders_per_day: int = 20         # 单日订单数上限
    max_daily_loss_pct: float = 0.03     # 日亏损熔断（较昨收权益）
    max_price_dev: float = 0.21          # 最新价偏离昨收上限（防脏数据，>20% 板幅即异常）
    blacklist: List[str] = field(default_factory=list)


@dataclass
class GuardResult:
    passed: List[OrderSpec] = field(default_factory=list)
    blocked: List[dict] = field(default_factory=list)  # {code, side, qty, reason}
    halted: bool = False
    halt_reason: str = ""


def check(plan: OrderPlan, cfg: RiskConfig, px: Dict[str, Quote],
          positions: List[Position],
          prev_equity: Optional[float] = None,
          orders_today: int = 0) -> GuardResult:
    res = GuardResult()

    # ---- 日级熔断 ----
    if prev_equity and prev_equity > 0:
        dd = plan.equity / prev_equity - 1
        if dd <= -cfg.max_daily_loss_pct:
            res.halted = True
            res.halt_reason = (f"日亏损熔断：权益 {plan.equity:.0f} 较昨收 "
                               f"{prev_equity:.0f} 回撤 {dd:.1%} ≥ {cfg.max_daily_loss_pct:.0%}")
            res.blocked = [{"code": o.code, "side": o.side, "qty": o.qty,
                            "reason": "日亏损熔断"} for o in plan.orders]
            return res

    blacklist = {to_std(c) for c in cfg.blacklist}
    remaining = max(cfg.max_orders_per_day - orders_today, 0)

    # 当前持仓市值（实时价）
    held: Dict[str, float] = {}
    for p in positions:
        q = px.get(to_std(p.code))
        held[p.code] = p.qty * (q.last if q and q.last > 0 else p.avg_cost)
    total_held = sum(held.values())

    # 逐单校验；通过的买单累计进仓位预估值，保证批内不超限
    est_held = dict(held)
    est_total = total_held

    for o in plan.orders:
        num = to_std(o.code)
        q = px.get(num)
        reason = None
        if remaining <= 0:
            reason = f"超单日订单数上限 {cfg.max_orders_per_day}"
        elif num in blacklist:
            reason = "黑名单标的"
        elif q is None:
            reason = "无行情，无法校验"
        elif q.prev_close <= 0:
            reason = "昨收异常"
        elif abs(q.last / q.prev_close - 1) > cfg.max_price_dev:
            reason = f"价格异常: {q.last} 偏离昨收超 {cfg.max_price_dev:.0%}"
        elif o.side == "buy" and plan.equity > 0:
            v = o.qty * q.last
            w_after = (est_held.get(o.code, 0.0) + v) / plan.equity
            t_after = (est_total + v) / plan.equity
            if w_after > cfg.max_weight_per_stock:
                reason = f"单票仓位将达 {w_after:.0%} > 上限 {cfg.max_weight_per_stock:.0%}"
            elif t_after > cfg.max_total_weight:
                reason = f"总仓位将达 {t_after:.0%} > 上限 {cfg.max_total_weight:.0%}"
            else:
                est_held[o.code] = est_held.get(o.code, 0.0) + v
                est_total += v
        elif o.side == "sell":
            est_held[o.code] = max(0.0, est_held.get(o.code, 0.0) - o.qty * q.last)
            est_total = max(0.0, est_total - o.qty * q.last)

        if reason:
            res.blocked.append({"code": o.code, "side": o.side, "qty": o.qty,
                                "reason": reason})
        else:
            res.passed.append(o)
            remaining -= 1
    return res
