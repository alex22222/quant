# -*- coding: utf-8 -*-
"""KDE 水平支撑阻力位算法实现（对应推文 Market Profile + KDE 方法）

核心流程（严格无未来视角，输入只用 t 日及之前的数据）：
1. 时间衰减：每个交易日权重 = 成交额 * 0.5^(距今交易日数/半衰期)
2. 筹码分布近似：当日成交额均匀摊在 [最低, 最高] 价格区间（Market Profile 思路）
3. KDE 平滑：高斯核，带宽 = bw_coef * ATR(14)（ATR 自适应带宽）
4. 峰值显著性过滤：scipy.find_peaks(prominence=...) 只保留强峰
"""
from pathlib import Path

import h5py
import numpy as np
import pandas as pd
from scipy.ndimage import gaussian_filter1d
from scipy.signal import find_peaks

BUNDLE = Path.home() / ".rqalpha" / "bundle"


class BundleData:
    """读取 RQAlpha 本地数据包，返回前复权日线 DataFrame"""

    def __init__(self):
        self._stocks = h5py.File(BUNDLE / "stocks.h5", "r")
        self._factors = h5py.File(BUNDLE / "ex_cum_factor.h5", "r")

    def list_stocks(self):
        return list(self._stocks.keys())

    def load(self, code, start=None, end=None):
        arr = self._stocks[code][:]
        df = pd.DataFrame(arr)
        df["date"] = pd.to_datetime(df["datetime"], format="%Y%m%d%H%M%S")
        df = df.set_index("date").sort_index()
        # 前复权：raw * factor / 最新factor
        fac = pd.DataFrame(self._factors[code][:])
        fac = fac[fac["start_date"] > 0]
        fac["date"] = pd.to_datetime(fac["start_date"], format="%Y%m%d%H%M%S")
        fac = fac.set_index("date").sort_index()["ex_cum_factor"]
        aligned = fac.reindex(df.index, method="ffill").fillna(1.0)
        adj = aligned / aligned.iloc[-1]
        for col in ["open", "close", "high", "low"]:
            df[col] = df[col] * adj
        df = df[df["volume"] > 0]  # 剔除停牌日
        if start:
            df = df.loc[pd.Timestamp(start):]
        if end:
            df = df.loc[:pd.Timestamp(end)]
        return df[["open", "close", "high", "low", "volume", "total_turnover"]]


def atr(df, n=14):
    """平均真实波幅"""
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def compute_levels(win, halflife=60, bw_coef=0.5, prom_ratio=0.12, grid_n=400):
    """在滚动窗口上计算支撑/阻力位

    win: 窗口内 DataFrame（只允许 t 日及之前的数据）
    返回: (levels: ndarray, prominences: ndarray, grid: ndarray, density: ndarray)
    """
    highs, lows = win["high"].values, win["low"].values
    turn = win["total_turnover"].values
    a = atr(win).iloc[-1]
    if not np.isfinite(a) or a <= 0:
        return np.array([]), np.array([]), None, None

    lo, hi = lows.min(), highs.max()
    pad = a * 2
    grid = np.linspace(lo - pad, hi + pad, grid_n)
    step = grid[1] - grid[0]

    # 1+2: 时间衰减加权的筹码分布直方图
    ages = np.arange(len(win))[::-1]  # 最后一天 age=0
    decay = 0.5 ** (ages / halflife)
    w = turn * decay
    hist = np.zeros(grid_n)
    i0 = np.clip(((lows - grid[0]) / step).astype(int), 0, grid_n - 1)
    i1 = np.clip(((highs - grid[0]) / step).astype(int), 0, grid_n - 1)
    for d in range(len(win)):
        span = i1[d] - i0[d] + 1
        hist[i0[d]:i1[d] + 1] += w[d] / span

    # 3: KDE（高斯核，ATR 自适应带宽）
    bw = max(bw_coef * a, step)
    density = gaussian_filter1d(hist, sigma=bw / step)

    # 4: 峰值显著性过滤
    peaks, props = find_peaks(density, prominence=prom_ratio * density.max(),
                              distance=max(int(a / step), 1))
    return grid[peaks], props["prominences"], grid, density
