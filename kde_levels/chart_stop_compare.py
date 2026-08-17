# -*- coding: utf-8 -*-
"""图表：三种止损机制的净值曲线对比 + 指标雷达"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(sys.executable).parent.parent.parent))
from daimon_runtime import setup_plot

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

setup_plot()

OUT = Path("/Users/henry/projects/quant/kde_levels/out")
styles = {
    "none": ("无止损（基线）", "#888888", 1.6),
    "atr":  ("ATR 吊灯止损", "#e5a50a", 1.6),
    "kde":  ("KDE 筹码位止损", "#1a5fb4", 1.8),
    "kde_profit": ("KDE 盈利仓止损", "#b06ab3", 1.8),
}

fig, (ax, axd) = plt.subplots(2, 1, figsize=(12, 8), sharex=True,
                              gridspec_kw={"height_ratios": [2.6, 1], "hspace": 0.08})

for m, (label, color, lw) in styles.items():
    p = pd.read_csv(OUT / f"portfolio_{m}.csv", parse_dates=["date"])
    ax.plot(p["date"], p["unit_net_value"], label=label, color=color, lw=lw)
    # 回撤
    nav = p["unit_net_value"]
    dd = nav / nav.cummax() - 1
    axd.plot(p["date"], dd * 100, color=color, lw=1.2)

ax.plot(p["date"], p["benchmark_unit_net_value"], label="沪深300", color="#c0c0c0", lw=1.2, ls="--")
ax.set_ylabel("单位净值")
ax.set_title("动量轮动策略：四种止损机制对比（2020-01 ~ 2026-07，10 万本金）", fontsize=13)
ax.legend(fontsize=10, loc="upper left")
ax.grid(alpha=0.2)

axd.set_ylabel("回撤 (%)")
axd.grid(alpha=0.2)
axd.axhline(0, color="#333", lw=0.6)

# 关键指标注释
sm = pd.read_csv(OUT / "stop_compare.csv")
txt = "\n".join(
    f"{styles[r['variant']][0]}: 年化 {r['年化%']}% | 回撤 {r['最大回撤%']}% | 夏普 {r['夏普']} | 换手 {r['年化双边换手']}"
    for _, r in sm.iterrows())
ax.text(0.985, 0.04, txt, transform=ax.transAxes, fontsize=9, ha="right",
        va="bottom", bbox=dict(boxstyle="round,pad=0.5", fc="white", ec="#cccccc", alpha=0.9))

fig.savefig(OUT / "stop_compare.png", dpi=200, bbox_inches="tight")
print("saved:", OUT / "stop_compare.png")
