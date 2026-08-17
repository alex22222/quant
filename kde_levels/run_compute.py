# -*- coding: utf-8 -*-
"""计算脚本（venv 运行）：
A. 可视化数据：600519 最近 250 日 + 当前水平位 + 密度曲线
B. 事件研究：60 只高流动性股票，触位后 N 日收益 vs 随机水平位对照 vs 无条件基准
输出 CSV 到 kde_levels/out/
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).parent))
from levels import BundleData, compute_levels, atr

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

WINDOW = 250
FORWARD = [5, 10, 20]
TOUCH_TOL_ATR = 0.3      # 触位容差 = 0.3 * ATR
LEVEL_NEAR_PCT = 0.12    # 只统计距现价 12% 以内的位（相关位）
RNG = np.random.default_rng(42)


def viz_data(bd, code="000858.XSHE"):
    df = bd.load(code).iloc[-WINDOW:]
    levels, proms, grid, density = compute_levels(df, prom_ratio=0.08)
    df.reset_index().to_csv(OUT / "viz_price.csv", index=False)
    pd.DataFrame({"price": grid, "density": density}).to_csv(OUT / "viz_density.csv", index=False)
    pd.DataFrame({"level": levels, "prominence": proms}).to_csv(OUT / "viz_levels.csv", index=False)
    print(f"[viz] {code} levels: {np.round(levels, 2)}")
    print(f"[viz] last close: {df['close'].iloc[-1]:.2f}")


def touches(df, levels, t, a):
    """判断第 t 日是否发生"支撑触位并守住"：位在昨收下方，今日最低触及，收盘守住"""
    prev_close = df["close"].iloc[t - 1]
    lo, close = df["low"].iloc[t], df["close"].iloc[t]
    hit = False
    for lv in levels:
        if lv >= prev_close:                      # 只看下方支撑位
            continue
        if prev_close - lv > LEVEL_NEAR_PCT * prev_close:  # 太远的位不算
            continue
        if lo <= lv + TOUCH_TOL_ATR * a and close > lv:
            hit = True
            break
    return hit


def event_study(bd, n_stocks=60, event_start="2022-01-01", event_end="2026-06-30"):
    # 选样：数据足够长 + 最近 250 日平均成交额最高的股票（高流动性）
    cand = []
    for code in bd.list_stocks():
        try:
            n = bd._stocks[code].shape[0]
            if n < 1500:
                continue
            tail = bd._stocks[code][-250:]
            to = tail["total_turnover"]
            cand.append((code, float(np.mean(to))))
        except Exception:
            continue
    cand.sort(key=lambda x: -x[1])
    picks = [c for c, _ in cand[:n_stocks]]
    print(f"[study] universe: {len(cand)} 只有效，取成交额前 {len(picks)} 只")

    rows = []
    for si, code in enumerate(picks):
        df = bd.load(code, end=event_end)
        a_ser = atr(df)
        dates = df.index
        mask = (dates >= event_start)
        idxs = np.where(mask)[0]
        for t in idxs:
            if t < WINDOW + 1 or t + max(FORWARD) >= len(df):
                continue
            a = a_ser.iloc[t]
            if not np.isfinite(a) or a <= 0:
                continue
            win = df.iloc[t - WINDOW:t]          # 严格只用 t-1 及之前 → 无未来视角
            levels, _, _, _ = compute_levels(win)
            close_t = df["close"].iloc[t]
            lo, hi = win["low"].min(), win["high"].max()
            # 随机对照：同数量随机水平位
            rand_levels = RNG.uniform(lo, hi, size=max(len(levels), 1))
            fwd = {k: df["close"].iloc[t + k] / close_t - 1 for k in FORWARD}
            rows.append({
                "code": code, "date": dates[t],
                "real_touch": touches(df, levels, t, a),
                "rand_touch": touches(df, rand_levels, t, a),
                **{f"fwd{k}": fwd[k] for k in FORWARD},
            })
        if (si + 1) % 10 == 0:
            print(f"[study] {si + 1}/{len(picks)} 只完成，累计 {len(rows)} 个样本日")

    res = pd.DataFrame(rows)
    res.to_csv(OUT / "event_study.csv", index=False)

    # 汇总统计
    summary = []
    for k in FORWARD:
        col = f"fwd{k}"
        real = res.loc[res["real_touch"], col]
        rand = res.loc[res["rand_touch"], col]
        base = res[col]
        t_stat, p = stats.ttest_ind(real, rand, equal_var=False)
        summary.append({
            "horizon": k,
            "n_real": len(real), "n_rand": len(rand), "n_all": len(base),
            "real_mean": real.mean(), "rand_mean": rand.mean(), "all_mean": base.mean(),
            "real_median": real.median(), "rand_median": rand.median(),
            "t_stat": t_stat, "p_value": p,
        })
    sm = pd.DataFrame(summary)
    sm.to_csv(OUT / "event_summary.csv", index=False)
    print(sm.to_string(index=False))


if __name__ == "__main__":
    bd = BundleData()
    viz_data(bd)
    event_study(bd)
