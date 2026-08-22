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
  - 量价单位自检：amount/(volume*100) 与 close 偏差 >30% 视为单位口径错误，拒绝写入
  - 涨跌停字段自检：新增行 high_limit/low_limit 必须包住 close（容差 0.5%），否则拒绝写入
  - 除权事件检测：append 窗口内若 collect 与 bundle 衔接日涨幅与 pct_chg 不符则告警
  - 写入后生成 patch_manifest.json（版本号 + 校验摘要 + 逐标的校验结果）
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = "patch_manifest.json"


def dt_int(d: str) -> int:
    return int(d.replace("-", "") + "000000")


def to_rq(code: str, secid: str) -> str:
    return f"{code}.XSHG" if secid.startswith("1.") else f"{code}.XSHE"


def patch_file(h5path: Path, key: str, klines: list[dict], is_stock: bool, apply: bool,
               report: dict) -> int:
    """校验并（可选）追加 K 线。校验结果写入 report[key]。

    返回追加行数；任何硬校验失败返回 0 且 report 记录 rejected 原因。
    """
    rep = report[key] = {"rows_added": 0, "checks": [], "rejected": None}
    h = h5py.File(h5path, "r+" if apply else "r")
    try:
        old = h[key][:]
        rep["dtype"] = str(old.dtype)
        last_dt = int(old["datetime"][-1])
        add = [k for k in klines if dt_int(k["date"]) > last_dt]
        overlap = [k for k in klines if dt_int(k["date"]) == last_dt]
        if overlap:
            oc, kc = float(old["close"][-1]), float(overlap[0]["close"])
            diff = abs(oc - kc) / oc
            rep["checks"].append({"check": "overlap_close", "diff_pct": round(diff * 100, 3),
                                  "pass": diff < 0.005})
            flag = "OK" if diff < 0.005 else "MISMATCH-拒写"
            print(f"  重叠日 {overlap[0]['date']}: bundle={oc} collect={kc} 偏差 {diff*100:.3f}% → {flag}")
            if diff >= 0.005:
                rep["rejected"] = f"重叠日收盘价偏差 {diff*100:.3f}%"
                return 0
        if not add:
            print(f"  {key}: 无新增（bundle 已最新）")
            return 0
        # 除权粗检：append 首日 collect 涨幅 vs 其自报 pct_chg
        first = add[0]
        implied = float(first["close"]) / float(old["close"][-1]) - 1
        stated = (first.get("pct_chg") or implied * 100) / 100
        if abs(implied - stated) > 0.01:
            rep["checks"].append({"check": "adjustment", "implied": round(implied * 100, 2),
                                  "stated": round(stated * 100, 2), "pass": False})
            print(f"  ⚠️ {key} append 首日隐含涨幅 {implied*100:+.2f}% vs 自报 {stated*100:+.2f}%——"
                  f"可能存在除权/复权口径问题，请人工确认")
        prev = float(old["close"][-1])
        rows = []
        for k in add:
            c = float(k["close"])
            amt = k.get("amount")
            amt = float(amt) if amt is not None else 0.0
            vol = float(k["volume"])
            # 量价单位自检：东财 volume 单位为手，amount 为元 → 隐含均价应≈close
            if vol > 0 and amt > 0:
                implied_px = amt / (vol * 100)
                dev = abs(implied_px - c) / c
                if dev > 0.30:
                    rep["rejected"] = (f"{k['date']} 量价单位口径异常: "
                                       f"隐含均价 {implied_px:.2f} vs close {c} 偏差 {dev*100:.0f}%")
                    rep["checks"].append({"check": "unit", "date": k["date"],
                                          "dev_pct": round(dev * 100, 1), "pass": False})
                    print(f"  ❌ {key}: {rep['rejected']} → 拒写")
                    return 0
            if is_stock:
                rate = 1.2 if key.split(".")[0].startswith(("300", "688")) else 1.1
                hi, lo = round(prev * rate, 2), round(prev * (2 - rate), 2)
                # 涨跌停字段自检：close 必须落在 [low_limit, high_limit]（容差 0.5%）
                if not (lo * 0.995 <= c <= hi * 1.005):
                    rep["rejected"] = (f"{k['date']} 涨跌停字段异常: "
                                       f"close={c} 不在 [{lo}, {hi}] 内")
                    rep["checks"].append({"check": "limit_field", "date": k["date"], "pass": False})
                    print(f"  ❌ {key}: {rep['rejected']} → 拒写")
                    return 0
                rows.append((dt_int(k["date"]), float(k["open"]), c, float(k["high"]),
                             float(k["low"]), prev, hi, lo, vol, amt))
            else:
                rows.append((dt_int(k["date"]), float(k["open"]), c, float(k["high"]),
                             float(k["low"]), prev, vol, amt))
            prev = c
        print(f"  {key}: 追加 {len(rows)} 个交易日: {rows[0][0]} → {rows[-1][0]}"
              f"（{'写入' if apply else 'dry-run'}）")
        rep["rows_added"] = len(rows)
        rep["window"] = [str(rows[0][0]), str(rows[-1][0])]
        rep["sha256_tail"] = hashlib.sha256(
            np.asarray(rows, dtype=old.dtype).tobytes()).hexdigest()[:16]
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


def _write_manifest(bundle: Path, context_date: str, report: dict, applied: bool) -> None:
    """写入补丁版本清单：版本号 + 校验摘要。"""
    path = bundle / MANIFEST
    manifest = {}
    if path.exists():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    version = datetime.now().strftime("%Y%m%d%H%M%S")
    manifest.setdefault("patches", []).append({
        "version": version,
        "context_date": context_date,
        "applied_at": datetime.now().isoformat(timespec="seconds"),
        "applied": applied,
        "report": report,
    })
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[manifest] 已写入 {path}（version={version}）")


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
    report: dict = {}
    for h5name, rq, local, is_stock in targets:
        jf = ctx / f"technical_{local}.json"
        if not jf.exists():
            print(f"[跳过] {local}: {jf} 不存在")
            continue
        d = json.load(open(jf, encoding="utf-8"))
        print(f"[{h5name}] {rq}（collect as_of {d['as_of']}）")
        total += patch_file(bundle / h5name, rq, d["recent_klines"], is_stock,
                            args.apply, report)
    rejected = [k for k, v in report.items() if v.get("rejected")]
    print(f"\n合计追加 {total} 行（{'已写入' if args.apply else 'dry-run，未写入'}）"
          f"；拒写 {len(rejected)} 个标的" + (f": {rejected}" if rejected else ""))
    # manifest 始终落盘（含 dry-run 校验报告，便于审计）
    _write_manifest(bundle, args.context, report, applied=args.apply)
    if rejected:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
