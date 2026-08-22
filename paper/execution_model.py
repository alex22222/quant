# -*- coding: utf-8 -*-
"""A 股撮合模型（改进计划 Phase 1）

规则：
- 信号日与成交日分离：T 日收盘生成信号 → 挂单；T+1 按开盘价成交（无开盘价时用收盘价并标注）；
- T+1 卖出限制：当日买入的数量次日才能卖；
- 整手：买入数量向下取整到 100 股；
- 涨跌停不可成交：T+1 开盘涨停则买单失败、跌停则卖单失败（±10%，主板口径，可配置）；
- 停牌不可成交（该日无行情行视为停牌）；
- 费用：佣金（默认万 2.5，最低 5 元）、印花税（卖出收，默认万 5）、过户费（默认万 0.2，双向）。
  费率全部走 FeeConfig，禁止散落在各模块。

所有函数纯计算、无副作用；撮合结果用 FillResult 表达，失败必须带 reject_reason。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FeeConfig:
    commission_rate: float = 0.00025   # 佣金万 2.5
    min_commission: float = 5.0        # 最低 5 元
    stamp_tax_rate: float = 0.0005     # 印花税万 5（仅卖出）
    transfer_fee_rate: float = 0.00002  # 过户费万 0.2（双向）


DEFAULT_FEES = FeeConfig()


@dataclass
class Bar:
    """单日行情快照（撮合所需最小字段）"""
    open: float | None = None
    close: float | None = None
    prev_close: float | None = None
    suspended: bool = False
    limit_pct: float = 0.10            # 主板 ±10%；创业板/科创板 ±20% 由调用方覆盖


@dataclass
class FillResult:
    filled: bool
    side: str                 # buy / sell
    qty: int = 0
    price: float = 0.0        # 实际成交价
    amount: float = 0.0       # 成交额（不含费）
    fee: float = 0.0          # 总费用
    reject_reason: str = ""
    price_type: str = ""      # next_open / fallback_close


def commission(amount: float, cfg: FeeConfig = DEFAULT_FEES) -> float:
    return max(amount * cfg.commission_rate, cfg.min_commission)


def total_fee(side: str, amount: float, cfg: FeeConfig = DEFAULT_FEES) -> float:
    fee = commission(amount, cfg) + amount * cfg.transfer_fee_rate
    if side == "sell":
        fee += amount * cfg.stamp_tax_rate
    return round(fee, 2)


def is_limit_up(bar: Bar) -> bool:
    return (bar.open is not None and bar.prev_close
            and bar.open >= bar.prev_close * (1 + bar.limit_pct) - 1e-9)


def is_limit_down(bar: Bar) -> bool:
    return (bar.open is not None and bar.prev_close
            and bar.open <= bar.prev_close * (1 - bar.limit_pct) + 1e-9)


def round_lot(qty: float) -> int:
    """买入整手（100 股）向下取整"""
    return max(int(qty // 100) * 100, 0)


def simulate_fill(side: str, qty: int, exec_bar: Bar,
                  cfg: FeeConfig = DEFAULT_FEES) -> FillResult:
    """在 exec_bar（T+1 日）模拟成交。

    - 停牌 → 拒绝；买遇开盘涨停 / 卖遇开盘跌停 → 拒绝；
    - 成交价优先开盘价，缺失时退化收盘价并标注 price_type=fallback_close。
    """
    if qty <= 0:
        return FillResult(False, side, reject_reason="qty<=0")
    if exec_bar.suspended or (exec_bar.open is None and exec_bar.close is None):
        return FillResult(False, side, reject_reason="停牌/无行情")
    if side == "buy" and is_limit_up(exec_bar):
        return FillResult(False, side, reject_reason="开盘涨停无法买入")
    if side == "sell" and is_limit_down(exec_bar):
        return FillResult(False, side, reject_reason="开盘跌停无法卖出")

    if exec_bar.open is not None:
        price, ptype = exec_bar.open, "next_open"
    else:
        price, ptype = exec_bar.close, "fallback_close"

    qty = round_lot(qty) if side == "buy" else int(qty)
    if qty <= 0:
        return FillResult(False, side, reject_reason="整手后为 0")
    amount = round(qty * price, 2)
    fee = total_fee(side, amount, cfg)
    return FillResult(True, side, qty=qty, price=round(price, 3),
                      amount=amount, fee=fee, price_type=ptype)


# ─────────────────────────────────────────────────────────────
# Bar 构造：从 BundleData 取指定日（或最新日）行情
# ─────────────────────────────────────────────────────────────

def bar_from_bundle(bd, code: str, day: str, index_latest: str | None = None) -> Bar:
    """day: YYYY-MM-DD。该日无行情行 → suspended=True。

    index_latest: 指数最新交易日；若给定且该股票最后交易日早于它，视为停牌。
    """
    try:
        df = bd.load(code)
    except Exception:
        return Bar(suspended=True)
    if index_latest is not None and df.index[-1].date().isoformat() < min(day, index_latest):
        return Bar(suspended=True)
    rows = df.loc[df.index.strftime("%Y-%m-%d") == day]
    if rows.empty:
        return Bar(suspended=True)
    i = df.index.get_loc(rows.index[0])
    prev_close = float(df["close"].iloc[i - 1]) if i > 0 else None
    open_ = float(rows["open"].iloc[0]) if "open" in rows.columns else None
    close = float(rows["close"].iloc[0])
    # 创业板(30xxxx)/科创板(688xxx) ±20%
    limit = 0.20 if code.startswith(("30", "688")) else 0.10
    return Bar(open=open_, close=close, prev_close=prev_close,
               suspended=False, limit_pct=limit)
