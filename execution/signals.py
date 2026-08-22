# -*- coding: utf-8 -*-
"""signals：目标仓位信号——与 pipeline/stage_paper.py 同一策略逻辑

v1：momentum_rotation（20 日动量取前 3，120 日线大盘风控，等权 0.98/N）。
信号与 paper 线同源，保证「回测 = 模拟 = 实盘」同一套策略定义。
"""
import sys
from pathlib import Path
from typing import Dict, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "kde_levels"))

from levels import BundleData  # noqa: E402

UNIVERSE = ["600519.XSHG", "000858.XSHE", "600036.XSHG", "601318.XSHG",
            "300750.XSHE", "002594.XSHE", "601899.XSHG", "000333.XSHE",
            "600900.XSHG", "601012.XSHG"]
MOM_DAYS, HOLD_NUM, MA_DAYS = 20, 3, 120
BENCH = "000300.XSHG"
TOTAL_WEIGHT = 0.98


def target_weights() -> Tuple[Dict[str, float], dict]:
    """返回 ({code: weight}, meta)。market_ok=False 时返回空 dict（清仓信号）。"""
    bd = BundleData()
    idx = bd.load_index(BENCH)
    idx_close = idx["close"].values
    market_ok = bool(idx_close[-1] > idx_close[-MA_DAYS:].mean())
    latest = idx.index[-1].date().isoformat()

    table = []
    for s in UNIVERSE:
        df = bd.load(s)
        c = df["close"].values
        if len(c) < MOM_DAYS + 1:
            continue
        table.append((s, c[-1] / c[-MOM_DAYS - 1] - 1))
    table.sort(key=lambda x: -x[1])

    targets = {}
    if market_ok:
        w = TOTAL_WEIGHT / HOLD_NUM
        for s, _ in table[:HOLD_NUM]:
            targets[s] = w
    meta = {"latest_trade_date": latest, "market_ok_120ma": market_ok,
            "momentum": [{"code": s, "mom": round(m, 4)} for s, m in table[:5]]}
    return targets, meta
