#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""二阶信号实验 · 加密版（复刻笔记方法论，适配 24/7 市场）

Part 1 二阶可预测性：15 个量价特征 + 手写 Ridge → 预测未来 5 日已实现波动率
        （横截面），滚动样本外 Rank IC + t 值。
Part 2 三层对比回测：
        (a) 动量 Top-8 等权（基准）
        (b) 权重层：Top-8 内部逆波动率调权 —— 笔记预言它跑输等权
        (c) 准入层：Score = z(mom20) − λ·z(σ̂) 取 Top-8 等权
        单边费率 10bps，每 5 日调仓，信号 t 日收盘、成交 t+1 收盘。
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

import sys
BASE = Path(__file__).resolve().parent
# 参数化：python experiment.py [data_dir] [bench] [ann_factor] [fee] [top_n] [out_json]
DATA = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "data"
BENCH = sys.argv[2] if len(sys.argv) > 2 else "BTC"
ANN = int(sys.argv[3]) if len(sys.argv) > 3 else 365
FEE = float(sys.argv[4]) if len(sys.argv) > 4 else 0.001
TOP_N = int(sys.argv[5]) if len(sys.argv) > 5 else 8
OUT_JSON = Path(sys.argv[6]) if len(sys.argv) > 6 else BASE / "result.json"
REBAL = 5            # 每 5 个交易日调仓
FWD = 5              # 预测目标：未来 5 日已实现波动
TRAIN_WIN = 250      # Ridge 滚动训练窗（天）
RETRAIN = 20         # 每 20 天重训一次
RIDGE_A = 10.0       # 正则强度
LAMBDAS = [0.5, 1.0] # 准入层风险惩罚系数


# ── 数据 ──────────────────────────────────────────────
def load_panel():
    frames = {}
    for f in sorted(DATA.glob("*.csv")):
        df = pd.read_csv(f, parse_dates=["date"]).set_index("date")
        frames[f.stem] = df
    # 对齐交易日（UTC 日，币圈天天交易）
    idx = frames[BENCH].index
    close = pd.DataFrame({k: v["close"].reindex(idx) for k, v in frames.items()}).ffill()
    high  = pd.DataFrame({k: v["high"].reindex(idx)  for k, v in frames.items()}).ffill()
    low   = pd.DataFrame({k: v["low"].reindex(idx)   for k, v in frames.items()}).ffill()
    amt   = pd.DataFrame({k: v["amount"].reindex(idx) for k, v in frames.items()}).ffill()
    return close, high, low, amt


# ── 特征（15 个，全量价，t 日收盘可见）─────────────────
def build_features(close, high, low, amt):
    ret = close.pct_change()
    logret = np.log(close / close.shift(1))
    rng = (high - low) / close
    F = {}
    for n in (1, 5, 10, 20):
        F[f"ret{n}"] = close.pct_change(n)
    for n in (5, 10, 20):
        F[f"vol{n}"] = logret.rolling(n).std() * np.sqrt(ANN)
    F["range5"] = rng.rolling(5).mean()
    F["range20"] = rng.rolling(20).mean()
    a5, a20 = amt.rolling(5).mean(), amt.rolling(20).mean()
    F["amt_ratio"] = a5 / a20
    F["amt_chg"] = a20.pct_change(5)
    hi20, lo20 = high.rolling(20).max(), low.rolling(20).min()
    F["pos20"] = (close - lo20) / (hi20 - lo20)
    F["maxdd20"] = close / close.rolling(20).max() - 1
    # 量价相关（20 日滚动相关系数，逐列算）
    pvc = pd.DataFrame(index=close.index, columns=close.columns, dtype=float)
    for c in close.columns:
        pvc[c] = ret[c].rolling(20).corr(amt[c].pct_change())
    F["pvcorr20"] = pvc
    return F


# ── Ridge（numpy 闭式解）──────────────────────────────
def ridge_fit(X, y, a=RIDGE_A):
    Xm, Xs = X.mean(0), X.std(0) + 1e-12
    ym, ys = y.mean(), y.std() + 1e-12
    Xn, yn = (X - Xm) / Xs, (y - ym) / ys
    n = Xn.shape[1]
    coef = np.linalg.solve(Xn.T @ Xn + a * np.eye(n), Xn.T @ yn)
    return {"coef": coef, "Xm": Xm, "Xs": Xs, "ym": ym, "ys": ys}


def ridge_pred(model, X):
    Xn = (X - model["Xm"]) / model["Xs"]
    return Xn @ model["coef"] * model["ys"] + model["ym"]


def spearman(a, b):
    ra = pd.Series(a).rank().values
    rb = pd.Series(b).rank().values
    if ra.std() < 1e-12 or rb.std() < 1e-12:
        return np.nan
    return np.corrcoef(ra, rb)[0, 1]


def zscore(df_row):
    return (df_row - df_row.mean()) / (df_row.std() + 1e-12)


# ── 主流程 ────────────────────────────────────────────
def main():
    close, high, low, amt = load_panel()
    coins = list(close.columns)
    dates = close.index
    logret = np.log(close / close.shift(1))
    fwd_vol = logret.shift(-1).rolling(FWD).std().shift(-(FWD - 1)) * np.sqrt(ANN)  # t+1..t+5

    F = build_features(close, high, low, amt)
    feat_names = list(F.keys())

    # ── Part 1: 滚动样本外 Rank IC ──
    start = TRAIN_WIN + 25
    preds = pd.DataFrame(np.nan, index=dates, columns=coins)
    model = None
    for i in range(start, len(dates) - FWD - 1):
        if model is None or (i - start) % RETRAIN == 0:
            lo, hi_ = i - TRAIN_WIN, i
            Xs_all, ys_all = [], []
            for j in range(lo, hi_):
                xrow = np.column_stack([F[k].iloc[j].values for k in feat_names])
                yrow = fwd_vol.iloc[j].values
                m = np.isfinite(xrow).all(1) & np.isfinite(yrow)
                if m.sum() >= 20:
                    Xs_all.append(xrow[m]); ys_all.append(yrow[m])
            model = ridge_fit(np.vstack(Xs_all), np.concatenate(ys_all))
        xrow = np.column_stack([F[k].iloc[i].values for k in feat_names])
        m = np.isfinite(xrow).all(1)
        if m.sum() >= 20:
            preds.iloc[i, m] = ridge_pred(model, xrow[m])

    ics = [spearman(preds.iloc[i].values, fwd_vol.iloc[i].values)
           for i in range(start, len(dates) - FWD - 1)]
    ics = pd.Series([x for x in ics if np.isfinite(x)])
    ic_mean, ic_t = ics.mean(), ics.mean() / (ics.std() / np.sqrt(len(ics)))

    # ── Part 2: 三层回测 ──
    mom = close.pct_change(20)
    bench_ma200 = close[BENCH].rolling(200).mean()      # 大盘风控：基准 200 日线
    sig_dates = list(range(start, len(dates) - FWD - 1, REBAL))
    strat_ret = {k: [] for k in ["a_eq", "b_wt", "a_mf"]
                 + [f"c_{l}" for l in LAMBDAS] + ["c_1.0_mf"]}
    ret_next = close.pct_change().shift(-1)  # t 日成交后，t+1 日收益

    cur_w = {k: pd.Series(0.0, index=coins) for k in strat_ret}
    for i in range(start, len(dates) - 1):
        for k in strat_ret:
            w = cur_w[k]
            day_r = float((w * ret_next.iloc[i].fillna(0)).sum())
            # 持仓漂移
            cur_w[k] = w * (1 + ret_next.iloc[i].fillna(0))
            tot = cur_w[k].sum()
            if tot > 0:
                cur_w[k] = cur_w[k] / tot
            strat_ret[k].append(day_r)
        if i in sig_dates:
            mrow, prow = mom.iloc[i], preds.iloc[i]
            valid = mrow.notna() & prow.notna()
            if valid.sum() < TOP_N + 2:
                continue
            mv, pv = mrow[valid], prow[valid]
            top_mom = mv.nlargest(TOP_N).index
            new_w = {}
            new_w["a_eq"] = pd.Series(1 / TOP_N, index=top_mom)
            iv = 1 / (pv[top_mom].clip(lower=0.05))
            new_w["b_wt"] = iv / iv.sum()          # 权重层：逆波动率调权
            for l in LAMBDAS:
                score = zscore(mv) - l * zscore(pv)  # 准入层：分数重排
                new_w[f"c_{l}"] = pd.Series(1 / TOP_N, index=score.nlargest(TOP_N).index)
            # 大盘风控变体：BTC 跌破 200 日线时空仓
            market_ok = bool(close[BENCH].iloc[i] > bench_ma200.iloc[i])
            new_w["a_mf"] = new_w["a_eq"] if market_ok else pd.Series(dtype=float)
            new_w["c_1.0_mf"] = new_w["c_1.0"] if market_ok else pd.Series(dtype=float)
            for k, nw in new_w.items():
                full = pd.Series(0.0, index=coins)
                full.loc[list(nw.index)] = nw.values   # .loc 强制按标签，杜绝位置歧义
                turn = (full - cur_w[k]).abs().sum()
                strat_ret[k][-1] -= turn * FEE     # 当日扣换手费
                cur_w[k] = full

    # ── 指标 ──
    def metrics(r):
        r = pd.Series(r)
        nav = (1 + r).cumprod()
        ann = nav.iloc[-1] ** (ANN / len(r)) - 1
        dd = (nav / nav.cummax() - 1).min()
        sharpe = r.mean() / (r.std() + 1e-12) * np.sqrt(ANN)
        return {"ann": ann, "maxdd": dd, "calmar": ann / abs(dd) if dd else np.nan,
                "sharpe": sharpe, "nav": nav.iloc[-1]}

    results = {k: metrics(v) for k, v in strat_ret.items()}
    bench_eq = (1 + pd.Series(strat_ret["a_eq"])).cumprod()
    excess = {}
    for k in strat_ret:
        nav_k = (1 + pd.Series(strat_ret[k])).cumprod()
        excess[k] = float((nav_k / bench_eq).iloc[-1] - 1)

    out = {
        "universe": len(coins), "days": len(dates), "oos_days": len(ics),
        "rank_ic": round(float(ic_mean), 3), "rank_ic_t": round(float(ic_t), 1),
        "results": {k: {m: round(v, 3) for m, v in r.items()} for k, r in results.items()},
        "excess_vs_a": {k: round(v, 3) for k, v in excess.items()},
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
