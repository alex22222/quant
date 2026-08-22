# -*- coding: utf-8 -*-
"""统一信号接口（改进计划 Phase 1）

目标：回测（rqalpha）、Paper（stage_paper）、日报提案共享同一套信号计算逻辑。

约定：每个可上线的策略模块必须暴露：

    PARAMS: dict                      # 策略参数（唯一存放处，禁止别处复制）
    FAMILY: str                       # 策略族（去重用，如 "momentum_rotation"）
    def generate_targets(data, params=None) -> SignalResult

其中 `data` 是满足下方 StrategyData 协议的适配器：
- Paper 侧用 BundleDataAdapter（kde_levels.BundleData 实现）
- rqalpha 侧用策略文件内的 history_bars 包装（见 momentum_rotation.py）

SignalResult.targets 是目标持仓代码列表（等权权重由调用方决定），
market_ok 表示风控是否允许持仓；detail 可放动量表等诊断信息。
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

ROOT = Path(__file__).resolve().parent.parent


class StrategyData(Protocol):
    """策略读取数据的统一接口。所有价格均为前复权收盘价序列（旧→新）。"""

    def closes(self, code: str, n: int) -> np.ndarray | None:
        """最近 n 根日线收盘价（不足 n 根返回 None）"""

    def index_closes(self, code: str, n: int) -> np.ndarray | None:
        """指数最近 n 根日线收盘价"""

    def is_suspended(self, code: str) -> bool:
        """最新交易日是否停牌"""

    def latest(self) -> str:
        """数据最新交易日 YYYY-MM-DD"""


@dataclass
class SignalResult:
    targets: list[str]
    market_ok: bool
    detail: dict = field(default_factory=dict)


def load_strategy(name: str):
    """按注册名加载策略模块并校验接口。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    mod = importlib.import_module(f"strategies.{name}")
    for attr in ("PARAMS", "generate_targets"):
        if not hasattr(mod, attr):
            raise AttributeError(f"策略 {name} 缺少统一接口成员 {attr}")
    return mod


class BundleDataAdapter:
    """kde_levels.BundleData → StrategyData 适配器（Paper / 研究用）。"""

    def __init__(self, bundle_data=None):
        if bundle_data is None:
            kde = ROOT / "kde_levels"
            if str(kde) not in sys.path:
                sys.path.insert(0, str(kde))
            from levels import BundleData  # noqa: E402
            bundle_data = BundleData()
        self._bd = bundle_data

    def closes(self, code: str, n: int) -> np.ndarray | None:
        try:
            df = self._bd.load(code)
        except Exception:
            return None
        c = df["close"].values
        return c if len(c) >= n else None

    def index_closes(self, code: str, n: int) -> np.ndarray | None:
        try:
            df = self._bd.load_index(code)
        except Exception:
            return None
        c = df["close"].values
        return c if len(c) >= n else None

    def is_suspended(self, code: str) -> bool:
        # bundle 中停牌日通常无行；以"该标的最后交易日是否等于指数最后交易日"近似判断
        try:
            df = self._bd.load(code)
            idx = self._bd.load_index("000300.XSHG")
            return df.index[-1] < idx.index[-1]
        except Exception:
            return True  # 读不出数据视为不可交易（fail closed）

    def latest(self) -> str:
        df = self._bd.load_index("000300.XSHG")
        return df.index[-1].date().isoformat()
