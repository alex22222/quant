# -*- coding: utf-8 -*-
"""图表 1：KDE 水平支撑阻力位可视化（五粮液 000858，最近 250 个交易日）
左：K线 + 水平位（线宽∝显著性）  右：筹码密度曲线（Market Profile 视角）
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(sys.executable).parent.parent.parent))
from daimon_runtime import setup_plot

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

setup_plot()

OUT = Path("/Users/henry/projects/quant/kde_levels/out")
price = pd.read_csv(OUT / "viz_price.csv", parse_dates=["date"])
dens = pd.read_csv(OUT / "viz_density.csv")
levels = pd.read_csv(OUT / "viz_levels.csv")

fig, (ax, axp) = plt.subplots(
    1, 2, figsize=(13, 6.5), sharey=True,
    gridspec_kw={"width_ratios": [3.2, 1], "wspace": 0.02},
)

x = np.arange(len(price))
# K线
for i, r in enumerate(price.itertuples()):
    up = r.close >= r.open
    c = "#d43f3a" if up else "#2e7d32"  # A股配色：红涨绿跌
    ax.plot([i, i], [r.low, r.high], color=c, lw=0.6, zorder=2)
    ax.add_patch(Rectangle((i - 0.3, min(r.open, r.close)), 0.6,
                           abs(r.close - r.open) + 1e-9, color=c, zorder=3))
ax.plot(x, price["close"], color="#333333", lw=0.8, alpha=0.6, zorder=4)

# 水平位：线宽与透明度随 prominence 增大
pmax = levels["prominence"].max()
for lv, pr in zip(levels["level"], levels["prominence"]):
    w = 1.2 + 3.5 * pr / pmax
    ax.axhline(lv, color="#1a5fb4", lw=w, alpha=0.45 + 0.45 * pr / pmax, zorder=1)
    ax.text(len(price) * 1.005, lv, f"{lv:.2f}", va="center", fontsize=9,
            color="#1a5fb4", clip_on=False)

last = price["close"].iloc[-1]
ax.axhline(last, color="#e5a50a", lw=1.2, ls="--", zorder=1)
ax.text(len(price) * 1.005, last, f"现价 {last:.2f}", va="center", fontsize=9,
        color="#e5a50a", clip_on=False)

# x 轴日期刻度
ticks = np.linspace(0, len(price) - 1, 6).astype(int)
ax.set_xticks(ticks)
ax.set_xticklabels([price["date"].iloc[i].strftime("%y-%m") for i in ticks])
ax.set_title("五粮液 000858 · KDE 筹码共识水平位（最近 250 日，前复权）", fontsize=13)
ax.set_xlim(-1, len(price))
ax.grid(alpha=0.2)

# 右侧密度剖面
axp.fill_betweenx(dens["price"], dens["density"], color="#1a5fb4", alpha=0.35)
axp.plot(dens["density"], dens["price"], color="#1a5fb4", lw=1)
for lv in levels["level"]:
    axp.axhline(lv, color="#1a5fb4", lw=0.8, alpha=0.4)
axp.set_title("时间加权\n筹码密度", fontsize=11)
axp.set_xticks([])
axp.grid(alpha=0.2)

fig.savefig(OUT / "levels_000858.png", dpi=200, bbox_inches="tight")
print("saved:", OUT / "levels_000858.png")
