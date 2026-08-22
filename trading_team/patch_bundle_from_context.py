#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 trading_team/context/<date>/ 的新鲜 K 线补写进 RQAlpha bundle。

背景：官方月度 bundle（rqbundle_YYYYMM）只更新到上月末，月中会出现数据空窗；
collect.py 每天已从东财/腾讯采好最新日线，本脚本把空窗期的 K 线追加进
bundle 的 indexes.h5 / stocks.h5，使 briefing.py 能产出最新数据包。

用法:
  .venv/bin/python trading_team/patch_bundle_from_context.py \
      --context 2026-08-22 --bundle ~/.rqalpha/bundle-fresh/bundle [--apply]

默认 dry-run（只校验、打印计划）；加 --apply 才真正写入。
安全约束:
  - 只在指定 --bundle 路径上操作，先 dry-run 看重叠日一致性
  - 重叠日（bundle 最后一天）收盘价偏差 >0.5% 时拒绝写入该标的
  - 除权事件检测：append 窗口内若 collect 与 bundle 衔接日涨幅与 pct_chg 不符则告警
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parent.parent


def dt_int(d: str) -> int:
    return int(d.replace("-", "") + "000000")


def to_rq(code: str, secid: str) -> str:
    return f"{code}.XSHG" if secid.startswith("1.") else f"{code}.XSHE"


def patch_file(h5path: Path, key: str, klines: list[dict], is_stock: bool, apply: bool) -> int:
    h = h5py.File(h5path, "r+" if apply else "r")
    try:
        old = h[key][:]
        last_dt = int(old["datetime"][-1])
        add = [k for k in klines if dt_int(k["date"]) > last_dt]
        overlap = [k for k in klines if dt_int(k["date"]) == last_dt]
        if overlap:
            oc, kc = float(old["close"][-1]), float(overlap[0]["close"])
            diff = abs(oc - kc) / oc
            flag = "OK" if diff < 0.005 else "MISMATCH-拒写"
            print(f"  重叠日 {overlap[0]['date']}: bundle={oc} collect={kc} 偏差 {diff*100:.3f}% → {flag}")
            if diff >= 0.005:
                return 0
        if not add:
            print(f"  {key}: 无新增（bundle 已最新）")
            return 0
        # 除权粗检：append 首日 collect 涨幅 vs 其自报 pct_chg
        first = add[0]
        implied = float(first["close"]) / float(old["close"][-1]) - 1
        stated = (first.get("pct_chg") or implied * 100) / 100
        if abs(implied - stated) > 0.01:
            print(f"  ⚠️ {key} append 首日隐含涨幅 {implied*100:+.2f}% vs 自报 {stated*100:+.2f}%——"
                  f"可能存在除权/复权口径问题，请人工确认")
        prev = float(old["close"][-1])
        rows = []
        for k in add:
            c = float(k["close"])
            amt = k.get("amount")
            amt = float(amt) if amt is not None else 0.0
            if is_stock:
                rate = 1.2 if key.split(".")[0].startswith(("300", "688")) else 1.1
                rows.append((dt_int(k["date"]), float(k["open"]), c, float(k["high"]),
                             float(k["low"]), prev, round(prev * rate, 2),
                             round(prev * (2 - rate), 2), float(k["volume"]), amt))
            else:
                rows.append((dt_int(k["date"]), float(k["open"]), c, float(k["high"]),
                             float(k["low"]), prev, float(k["volume"]), amt))
            prev = c
        print(f"  {key}: 追加 {len(rows)} 个交易日: {rows[0][0]} → {rows[-1][0]}"
              f"（{'写入' if apply else 'dry-run'}）")
        if apply:
            new = np.concatenate([old, np.array(rows, dtype=old.dtype)])
            tmp = key + "_tmp"
            if tmp in h:
                del h[tmp]
            h.create_dataset(tmp, data=new)
            del h[key]
            h.move(tmp, key)
        return len(rows)
    finally:
        h.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", required=True, help="context 日期目录，如 2026-08-22")
    ap.add_argument("--bundle", required=True, help="目标 bundle 目录（含 indexes.h5/stocks.h5）")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    args = ap.parse_args()

    ctx = ROOT / "trading_team" / "context" / args.context
    bundle = Path(args.bundle).expanduser()
    wl = json.load(open(ROOT / "trading_team" / "watchlist.json", encoding="utf-8"))

    targets: list[tuple[str, str, str, bool]] = []  # (h5文件名, rq代码, 本地代码, 是否股票)
    bench = wl["benchmark"]
    targets.append(("indexes.h5", f"{bench['code']}.XSHG", bench["code"], False))
    for s in wl["stocks"]:
        targets.append(("stocks.h5", to_rq(s["code"], s["secid"]), s["code"], True))

    total = 0
    for h5name, rq, local, is_stock in targets:
        jf = ctx / f"technical_{local}.json"
        if not jf.exists():
            print(f"[跳过] {local}: {jf} 不存在")
            continue
        d = json.load(open(jf, encoding="utf-8"))
        print(f"[{h5name}] {rq}（collect as_of {d['as_of']}）")
        total += patch_file(bundle / h5name, rq, d["recent_klines"], is_stock, args.apply)
    print(f"\n合计追加 {total} 行（{'已写入' if args.apply else 'dry-run，未写入'}）")


if __name__ == "__main__":
    main()
