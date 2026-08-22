# -*- coding: utf-8 -*-
"""order_manager：目标仓位 → 可执行订单

规则：
  - 整手：100 股整数倍（不足一手不动）
  - T+1：卖出量 ≤ 券商返回的可卖数量（今日买入不可卖）
  - 涨跌停：涨停价挂买单/跌停价挂卖单直接拦截（不可能成交）
  - 停牌/无行情：跳过并记录原因
  - 先卖后买：买单预算 = 可用资金 + 本批卖单预估回款
  - 偏离阈值：目标市值与现值偏差 < equity × min_trade_pct 不动（防碎单）
  - 滑点：买单按 last×(1+slippage) 限价（封顶涨停价），卖单反之
"""
from dataclasses import dataclass, field
from typing import Dict, List

from .broker_base import Balance, Position, to_std
from .market import Quote


@dataclass
class OrderSpec:
    code: str        # RQAlpha 格式
    side: str        # buy / sell
    qty: int
    price: float     # 限价（含滑点）
    reason: str


@dataclass
class OrderPlan:
    orders: List[OrderSpec] = field(default_factory=list)
    skipped: List[dict] = field(default_factory=list)   # {code, reason}
    equity: float = 0.0


def _round2(x: float) -> float:
    return round(x + 1e-9, 2)


def build_plan(targets: Dict[str, float], balance: Balance,
               positions: List[Position], px: Dict[str, Quote],
               slippage: float = 0.002, min_trade_pct: float = 0.02,
               lot: int = 100) -> OrderPlan:
    """targets: {rq_code: 目标权重(0~1)}，空 dict = 清仓"""
    plan = OrderPlan()
    pos_by_code = {p.code: p for p in positions}

    # 市值以实时行情重估（无行情的持仓用成本价兜底）
    mv = 0.0
    for p in positions:
        q = px.get(to_std(p.code))
        mv += p.qty * (q.last if q and q.last > 0 else p.avg_cost)
    equity = balance.cash + mv
    plan.equity = equity
    if equity <= 0:
        plan.skipped.append({"code": "*", "reason": "权益为 0，无法建仓"})
        return plan

    def tgt_val(code):
        return equity * targets.get(code, 0.0)

    sells, buys = [], []

    # 卖出：不在目标内 → 清仓；在目标内 → 减到目标
    for code, p in pos_by_code.items():
        q = px.get(to_std(code))
        if not q or q.suspended:
            plan.skipped.append({"code": code, "reason": "停牌/无行情，卖出跳过"})
            continue
        diff = p.qty * q.last - tgt_val(code)
        if diff < equity * min_trade_pct:
            continue
        qty = int(diff / q.last / lot) * lot
        qty = min(qty, p.available)           # T+1 可卖约束
        if qty <= 0:
            if diff >= lot * q.last:
                plan.skipped.append({"code": code,
                                     "reason": f"T+1 可卖不足（可卖 {p.available}）"})
            continue
        if q.last <= q.limit_down:
            plan.skipped.append({"code": code, "reason": "跌停，卖出拦截"})
            continue
        price = _round2(max(q.limit_down, q.last * (1 - slippage)))
        sells.append(OrderSpec(code, "sell", qty, price, "调出名单/再平衡"))

    # 买入：目标内补仓或新建仓
    sell_proceeds = sum(s.qty * (px[to_std(s.code)].last) for s in sells)
    budget = balance.cash + sell_proceeds
    for code, w in sorted(targets.items(), key=lambda kv: -kv[1]):
        if w <= 0:
            continue
        q = px.get(to_std(code))
        if not q or q.suspended:
            plan.skipped.append({"code": code, "reason": "停牌/无行情，买入跳过"})
            continue
        cur = pos_by_code.get(code)
        cur_val = cur.qty * q.last if cur else 0.0
        diff = tgt_val(code) - cur_val
        if diff < equity * min_trade_pct:
            continue
        qty = int(diff / q.last / lot) * lot
        if qty <= 0:
            plan.skipped.append({"code": code, "reason": "偏差不足一手，跳过"})
            continue
        if q.last >= q.limit_up:
            plan.skipped.append({"code": code, "reason": "涨停，买入拦截"})
            continue
        affordable = int(budget / q.last / lot) * lot
        qty = min(qty, affordable)
        if qty <= 0:
            plan.skipped.append({"code": code, "reason": "资金不足一手，跳过"})
            continue
        price = _round2(min(q.limit_up, q.last * (1 + slippage)))
        budget -= qty * q.last
        buys.append(OrderSpec(code, "buy", qty, price, "动量入选/再平衡"))

    plan.orders = sells + buys   # 先卖后买
    return plan
