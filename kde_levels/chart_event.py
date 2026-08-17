# -*- coding: utf-8 -*-
"""图表 2：事件研究结果
真实 KDE 位触位 vs 随机水平位触位 vs 全部交易日，5/10/20 日前向收益对比
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(sys.executable).parent.parent.parent))
from daimon_runtime import setup_plot

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns

setup_plot()

OUT = Path("/Users/henry/projects/quant/kde_levels/out")
sm = pd.read_csv(OUT / "event_summary.csv")

groups = ["real", "rand", "all"]
labels = ["KDE位触位", "随机位触位(对照)", "全部交易日(基准)"]
colors = ["#1a5fb4", "#8f8f8f", "#c9c9c9"]
horizons = sm["horizon"].tolist()

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5.2),
                               gridspec_kw={"width_ratios": [1.4, 1]})

# 左：均值柱状图（%）
x = np.arange(len(horizons))
w = 0.26
for gi, g in enumerate(groups):
    vals = sm[f"{g}_mean"] * 100
    ax1.bar(x + (gi - 1) * w, vals, w, label=labels[gi], color=colors[gi])
    for xi, v in zip(x + (gi - 1) * w, vals):
        ax1.text(xi, v + 0.05, f"{v:.2f}", ha="center", fontsize=8.5)
ax1.set_xticks(x)
ax1.set_xticklabels([f"{h} 日前向收益" for h in horizons])
ax1.set_ylabel("平均收益 (%)")
ax1.axhline(0, color="#333", lw=0.8)
ax1.legend(fontsize=9)
ax1.set_title("触位后的平均前向收益：KDE 位并未跑赢随机位", fontsize=13)
ax1.grid(axis="y", alpha=0.2)

# 右：p 值与样本量
pvals = sm["p_value"]
bars = ax2.barh([f"{h} 日" for h in horizons], pvals, color="#b06ab3", alpha=0.8)
ax2.axvline(0.05, color="#d43f3a", ls="--", lw=1.2)
ax2.text(0.052, -0.45, "p=0.05 显著性门槛", color="#d43f3a", fontsize=9)
for b, p, n in zip(bars, pvals, sm["n_real"]):
    ax2.text(b.get_width() + 0.01, b.get_y() + b.get_height() / 2,
             f"p={p:.2f}  (n={n})", va="center", fontsize=9)
ax2.set_xlim(0, 1.05)
ax2.set_xlabel("p 值（KDE位 vs 随机位，Welch t 检验）")
ax2.set_title("统计显著性：三个周期全部不显著", fontsize=13)
ax2.grid(axis="x", alpha=0.2)

fig.suptitle("事件研究：60 只高流动性 A 股 · 2022-01 ~ 2026-06 · 63,841 个样本日",
             fontsize=11, y=0.02, color="#666")
fig.savefig(OUT / "event_study.png", dpi=200, bbox_inches="tight")
print("saved:", OUT / "event_study.png")
