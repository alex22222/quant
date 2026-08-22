# -*- coding: utf-8 -*-
"""execution 风控与订单管理边界用例（无网络依赖，离线可跑）

运行：.venv/bin/python execution/tests/test_guard.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from execution.broker_base import Balance, Position  # noqa: E402
from execution.market import Quote  # noqa: E402
from execution.order_manager import OrderPlan, OrderSpec, build_plan  # noqa: E402
from execution.risk_guard import RiskConfig, check  # noqa: E402


def q(last, prev=10.0, name="X", suspended=False):
    return Quote(code="X", name=name, last=last, prev_close=prev,
                 limit_up=round(prev * 1.1, 2), limit_down=round(prev * 0.9, 2),
                 suspended=suspended)


cfg = RiskConfig()

# 1) 涨停买入拦截（600036：昨收 9.09 最新 10.00 = 涨停价）
plan = build_plan({"600036.XSHG": 0.3},
                  Balance(cash=100000, total_asset=100000, market_value=0),
                  [], {"600036": q(10.0, 9.09)}, min_trade_pct=0.0)
assert any("涨停" in s["reason"] for s in plan.skipped), plan.skipped

# 1b) 跌停卖出拦截
plan = build_plan({}, Balance(cash=0, total_asset=10000, market_value=10000),
                  [Position(code="600036.XSHG", qty=1000, available=1000, avg_cost=10)],
                  {"600036": q(9.0, 10.0)}, min_trade_pct=0.0)
assert any("跌停" in s["reason"] for s in plan.skipped), (plan.orders, plan.skipped)

# 2) 日亏损熔断
plan2 = OrderPlan(orders=[OrderSpec("600036.XSHG", "sell", 100, 9.5, "t")], equity=96000)
res = check(plan2, cfg, {"600036": q(9.6)}, [], prev_equity=100000)
assert res.halted and "熔断" in res.halt_reason

# 3) 黑名单
plan3 = OrderPlan(orders=[OrderSpec("600036.XSHG", "buy", 100, 10.0, "t")], equity=100000)
res = check(plan3, RiskConfig(blacklist=["600036.XSHG"]), {"600036": q(10.0)}, [])
assert res.blocked and "黑名单" in res.blocked[0]["reason"]

# 4) 脏价格（偏离昨收 >21%）
res = check(plan3, cfg, {"600036": q(13.0, 10.0)}, [])
assert res.blocked and "价格异常" in res.blocked[0]["reason"]

# 5) 单票仓位上限（批内累计）
plan5 = OrderPlan(orders=[OrderSpec("600036.XSHG", "buy", 200, 10.0, "t"),
                          OrderSpec("600036.XSHG", "buy", 200, 10.0, "t")],
                  equity=10000)
res = check(plan5, cfg, {"600036": q(10.0)}, [])
assert len(res.passed) == 1 and len(res.blocked) == 1, (res.passed, res.blocked)

# 6) 单日订单数上限
res = check(plan5, cfg, {"600036": q(10.0)}, [], orders_today=20)
assert len(res.passed) == 0 and "订单数上限" in res.blocked[0]["reason"]

# 7) T+1 可卖约束
plan7 = build_plan({}, Balance(cash=0, total_asset=10000, market_value=10000),
                   [Position(code="600036.XSHG", qty=200, available=100, avg_cost=10)],
                   {"600036": q(10.0)}, min_trade_pct=0.0)
assert plan7.orders[0].qty == 100, plan7.orders

# 8) 停牌跳过
plan8 = build_plan({}, Balance(cash=0, total_asset=1, market_value=1),
                   [Position(code="600036.XSHG", qty=100, available=100, avg_cost=10)],
                   {"600036": q(0.0, 10.0, suspended=True)}, min_trade_pct=0.0)
assert not plan8.orders and "停牌" in plan8.skipped[0]["reason"]

# 9) 清仓信号（120 日线下方 → 空仓语义）
plan9 = build_plan({}, Balance(cash=5000, total_asset=15000, market_value=10000),
                   [Position(code="600036.XSHG", qty=1000, available=1000, avg_cost=10)],
                   {"600036": q(10.0)})
assert len(plan9.orders) == 1 and plan9.orders[0].side == "sell" \
    and plan9.orders[0].qty == 1000

print("== execution 风控/订单管理 10 条边界用例全部通过 ==")
