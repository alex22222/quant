# -*- coding: utf-8 -*-
"""券商通道抽象层 + 代码格式转换

项目内部统一使用 RQAlpha 格式 "600519.XSHG"；
easytrader/同花顺使用 6 位纯数字 "600519"；
腾讯行情使用小写前缀 "sh600519"。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------- 代码转换 ----------

def to_std(code: str) -> str:
    """任意格式 → 6 位纯数字"""
    c = code.strip().upper()
    if "." in c:
        return c.split(".")[0]
    for p in ("SH", "SZ", "BJ"):
        if c.startswith(p):
            return c[2:]
    return c


def to_rqalpha(code: str) -> str:
    """任意格式 → RQAlpha 格式"""
    num = to_std(code)
    if num[0] in ("6", "5", "9"):
        return f"{num}.XSHG"
    if num[0] in ("0", "1", "2", "3"):
        return f"{num}.XSHE"
    return f"{num}.XBJE"


def to_tencent(code: str) -> str:
    """任意格式 → 腾讯行情格式 sh600519 / sz000858"""
    num = to_std(code)
    prefix = "sh" if num[0] in ("6", "5", "9") else "sz"
    return prefix + num


def limit_pct(code: str, name: str = "") -> float:
    """涨跌停幅度（普通股票 10%，创业/科创 20%，北交所 30%，ST 5%）"""
    if "ST" in name.upper():
        return 0.05
    num = to_std(code)
    if num.startswith("30") or num.startswith("68"):
        return 0.20
    if num[0] in ("4", "8") or num.startswith("92"):
        return 0.30
    return 0.10


# ---------- 数据结构 ----------

@dataclass
class Balance:
    cash: float            # 可用资金
    total_asset: float     # 总资产
    market_value: float    # 持仓市值
    frozen: float = 0.0


@dataclass
class Position:
    code: str              # RQAlpha 格式
    qty: int               # 持仓数量
    available: int         # 可卖数量（T+1：今日买入不可卖）
    avg_cost: float
    last_price: float = 0.0


@dataclass
class OrderResult:
    ok: bool
    order_id: str = ""     # 券商合同编号（mock 时为本地单号）
    message: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class Entrust:
    """当日委托回报"""
    order_id: str
    code: str
    side: str              # buy / sell
    qty: int
    price: float
    status: str            # 已成 / 已撤 / 部成 / 未成交(已报) ...
    filled_qty: int = 0
    filled_price: float = 0.0


class Broker(ABC):
    """券商通道统一接口。所有实现必须幂等友好：断线重连后状态以券商侧查询为准。"""

    name: str = "abstract"

    @abstractmethod
    def connect(self) -> None:
        """建立/恢复连接。多次调用安全。"""

    @abstractmethod
    def balance(self) -> Balance: ...

    @abstractmethod
    def positions(self) -> List[Position]: ...

    @abstractmethod
    def buy(self, code: str, price: float, qty: int) -> OrderResult: ...

    @abstractmethod
    def sell(self, code: str, price: float, qty: int) -> OrderResult: ...

    @abstractmethod
    def cancel(self, order_id: str) -> OrderResult: ...

    @abstractmethod
    def today_entrusts(self) -> List[Entrust]: ...

    def close(self) -> None:
        """可选清理"""
