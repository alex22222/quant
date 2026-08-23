# -*- coding: utf-8 -*-
"""历史时点可投资股票池构建（策略库审查 P2 一期）

消除"固定 10 只蓝筹"的事后选择偏差。所有过滤条件均为**当日可见**信息：

- 上市满 365 个自然日（instruments.pk listed_date）
- 当日非 ST（st_stock_days.h5 逐日记录，point-in-time）
- 当日未停牌（suspended_days.h5）
- 当日有成交，且近 20 个交易日日均成交额 ≥ min_turnover
- 按 20 日日均成交额降序取前 n（确定性：并列按 code 字典序）

快照落盘 pipeline/universe_snapshots/YYYY-MM-DD.json 并提交 git——
每个调仓日的股票池可复现、可审计（审查 P2 第 3 条）。

用法：
  python -m pipeline.universe 2024-01-02            # 打印当日股票池
  python -m pipeline.universe --snapshots 2019-12-01 2026-08-01   # 生成月度快照
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import pickle
from datetime import datetime
from pathlib import Path

import h5py
import numpy as np

from .common import ROOT

BUNDLE = Path(os.path.expanduser("~/.rqalpha/bundle"))
SNAP_DIR = ROOT / "pipeline" / "universe_snapshots"


class UniverseBuilder:
    """point-in-time 股票池构建器（重复调用请复用同一实例，内部有缓存）。"""

    def __init__(self, bundle: Path = BUNDLE):
        self._bundle = bundle
        instruments = pickle.load(open(bundle / "instruments.pk", "rb"))
        # code -> listed_date(datetime.date)；只保留 A 股普通股票
        self._listed = {
            i["order_book_id"]: datetime.strptime(i["listed_date"], "%Y-%m-%d").date()
            for i in instruments if i.get("type") == "CS"
        }
        self._st = self._load_day_set(bundle / "st_stock_days.h5")
        self._susp = self._load_day_set(bundle / "suspended_days.h5")
        self._stocks = h5py.File(bundle / "stocks.h5", "r")
        self._dt_cache: dict[str, np.ndarray] = {}

    @staticmethod
    def _load_day_set(path: Path) -> dict[str, set]:
        f = h5py.File(path, "r")
        return {k: set(int(x) for x in f[k][:]) for k in f.keys()}

    def _datetimes(self, code: str) -> np.ndarray | None:
        if code not in self._dt_cache:
            ds = self._stocks.get(code)
            if ds is None:
                return None
            self._dt_cache[code] = ds["datetime"][:]
        return self._dt_cache[code]

    def is_st(self, code: str, day_int: int) -> bool:
        return day_int in self._st.get(code, ())

    def is_suspended(self, code: str, day_int: int) -> bool:
        return day_int in self._susp.get(code, ())

    def listed_enough(self, code: str, day: str, min_days: int = 365) -> bool:
        ld = self._listed.get(code)
        if ld is None:
            return False
        return (datetime.strptime(day, "%Y-%m-%d").date() - ld).days >= min_days

    def turnover_20d(self, code: str, day_int: int):
        """当日及前 19 个交易日的日均成交额；当日无交易/数据不足返回 None。"""
        dts = self._datetimes(code)
        if dts is None:
            return None
        ts = day_int * 1000000  # stocks.h5 的 datetime 为 YYYYMMDDhhmmss
        idx = bisect.bisect_right(dts, ts)
        if idx == 0 or int(dts[idx - 1]) != ts:
            return None  # 当日未交易（含已退市：数据终止后自然排除）
        if idx < 20:
            return None
        t = self._stocks[code]["total_turnover"][idx - 20:idx]
        if len(t) < 20 or not np.all(np.isfinite(t)):
            return None
        return float(np.mean(t))

    def build(self, day: str, n: int = 100, min_turnover: float = 5e7,
              min_listed_days: int = 365) -> list[str]:
        """返回 day 当日可投资股票池（按流动性降序，长度 ≤ n）。"""
        day_int = int(day.replace("-", ""))
        scored = []
        for code in self._listed:
            if not self.listed_enough(code, day, min_listed_days):
                continue
            if self.is_st(code, day_int) or self.is_suspended(code, day_int):
                continue
            t = self.turnover_20d(code, day_int)
            if t is None or t < min_turnover:
                continue
            scored.append((code, t))
        # 确定性排序：成交额降序，并列按 code
        scored.sort(key=lambda x: (-x[1], x[0]))
        return [c for c, _ in scored[:n]]

    def close(self):
        self._stocks.close()


def save_snapshot(builder: UniverseBuilder, day: str, **kw) -> Path:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    codes = builder.build(day, **kw)
    payload = {"day": day, "count": len(codes), "params": kw, "codes": codes}
    path = SNAP_DIR / f"{day}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_snapshot(day: str) -> list[str] | None:
    """读取已保存的股票池快照；不存在返回 None（fail closed：调用方必须显式处理）。"""
    path = SNAP_DIR / f"{day}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["codes"]


def _month_starts(start: str, end: str) -> list[str]:
    """月首日历日列表（build 内部用当日实际数据，非交易日会自动剔除无成交股）。"""
    dates = []
    y, m = int(start[:4]), int(start[5:7])
    while f"{y:04d}-{m:02d}-01" <= end:
        dates.append(f"{y:04d}-{m:02d}-01")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return dates


def main():
    ap = argparse.ArgumentParser(prog="pipeline.universe")
    ap.add_argument("day", nargs="?", help="单日构建，如 2024-01-02")
    ap.add_argument("--snapshots", nargs=2, metavar=("START", "END"),
                    help="生成月度快照区间")
    ap.add_argument("-n", type=int, default=100)
    ap.add_argument("--min-turnover", type=float, default=5e7)
    args = ap.parse_args()

    b = UniverseBuilder()
    try:
        if args.snapshots:
            days = _month_starts(args.snapshots[0], args.snapshots[1])
            for d in days:
                p = save_snapshot(b, d, n=args.n, min_turnover=args.min_turnover)
                print(f"{d}: {json.loads(p.read_text())['count']} 只 → {p.name}")
        elif args.day:
            codes = b.build(args.day, n=args.n, min_turnover=args.min_turnover)
            print(json.dumps({"day": args.day, "count": len(codes),
                              "top10": codes[:10]}, ensure_ascii=False, indent=2))
    finally:
        b.close()


if __name__ == "__main__":
    main()
