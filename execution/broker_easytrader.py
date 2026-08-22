# -*- coding: utf-8 -*-
"""EasyTrader 适配层：平安证券（同花顺通用下单客户端 universal_client）

⚠️ 运行前提：
  - Windows（easytrader 依赖 pywinauto 操控 Windows GUI，macOS 不可用）
  - 已安装同花顺下单客户端 xiadan.exe，并用平安证券资金账号登录过一次
  - pip install easytrader

字段口径（easytrader universal_client 返回）：
  balance:   {资金余额, 可用金额, 总资产, 股票市值, ...}
  position:  {证券代码, 证券名称, 股票余额, 可用股份, 成本价, 最新价, ...}
  entrusts:  {合同编号, 证券代码, 买卖方向/操作, 委托数量, 委托价格, 委托状态/备注, 成交数量, 成交均价}

easytrader 个别版本字段名有差异，_col() 做多候选名兜底。
"""
from typing import List

from .broker_base import (Balance, Broker, Entrust, OrderResult, Position,
                          to_rqalpha)


def _col(row: dict, *names, default=None):
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return default


class EasyTraderBroker(Broker):
    name = "easytrader"

    def __init__(self, client_path: str = None):
        """client_path: xiadan.exe 路径；None 则自动寻找已运行的同花顺客户端"""
        self._client_path = client_path
        self._user = None

    def connect(self) -> None:
        if self._user is not None:
            return
        try:
            import easytrader
        except ImportError as e:
            raise RuntimeError(
                "easytrader 未安装。实盘通道需要 Windows 环境：pip install easytrader"
            ) from e
        user = easytrader.use("universal_client")
        user.connect(self._client_path)  # None=自动匹配已运行客户端
        user.enable_type_keys_for_editor()
        self._user = user

    def _need(self):
        if self._user is None:
            self.connect()
        return self._user

    def balance(self) -> Balance:
        rows = self._need().balance or [{}]
        r = rows[0] if isinstance(rows, list) else rows
        return Balance(
            cash=float(_col(r, "可用金额", "资金余额", default=0)),
            total_asset=float(_col(r, "总资产", "资产总值", default=0)),
            market_value=float(_col(r, "股票市值", "最新市值", default=0)),
            frozen=float(_col(r, "冻结金额", default=0)),
        )

    def positions(self) -> List[Position]:
        out = []
        for r in (self._need().position or []):
            out.append(Position(
                code=to_rqalpha(str(_col(r, "证券代码", "股票代码"))),
                qty=int(float(_col(r, "股票余额", "当前持仓", default=0))),
                available=int(float(_col(r, "可用股份", "可用余额", default=0))),
                avg_cost=float(_col(r, "成本价", "参考成本价", default=0)),
                last_price=float(_col(r, "最新价", default=0)),
            ))
        return [p for p in out if p.qty > 0]

    def buy(self, code: str, price: float, qty: int) -> OrderResult:
        return self._wrap(self._need().buy, code, price, qty)

    def sell(self, code: str, price: float, qty: int) -> OrderResult:
        return self._wrap(self._need().sell, code, price, qty)

    def _wrap(self, fn, code, price, qty) -> OrderResult:
        num = code.split(".")[0]
        try:
            ret = fn(num, price=price, amount=qty) or {}
            return OrderResult(ok=True, order_id=str(ret.get("合同编号", "")),
                               message=str(ret), raw=ret)
        except Exception as e:
            return OrderResult(ok=False, message=f"{type(e).__name__}: {e}")

    def cancel(self, order_id: str) -> OrderResult:
        try:
            ret = self._need().cancel_entrust(order_id) or {}
            return OrderResult(ok=True, order_id=order_id, message=str(ret),
                               raw=ret if isinstance(ret, dict) else {})
        except Exception as e:
            return OrderResult(ok=False, message=f"{type(e).__name__}: {e}")

    def today_entrusts(self) -> List[Entrust]:
        out = []
        for r in (self._need().today_entrusts or []):
            side_raw = str(_col(r, "操作", "买卖方向", default=""))
            side = "buy" if "买" in side_raw else "sell"
            out.append(Entrust(
                order_id=str(_col(r, "合同编号", default="")),
                code=to_rqalpha(str(_col(r, "证券代码", default=""))),
                side=side,
                qty=int(float(_col(r, "委托数量", default=0))),
                price=float(_col(r, "委托价格", default=0)),
                status=str(_col(r, "备注", "委托状态", default="")),
                filled_qty=int(float(_col(r, "成交数量", default=0) or 0)),
                filled_price=float(_col(r, "成交均价", default=0) or 0),
            ))
        return out
