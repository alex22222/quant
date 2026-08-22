# -*- coding: utf-8 -*-
"""实时行情：腾讯 qt.gtimg.cn（主）——用于下单定价与涨跌停判断

实盘定价语义：以最新价为基础加滑点挂限价单（买单向上、卖单向下），
避免市价扫单失控。涨跌停价按昨收 ±limit_pct 估算（除权日会有误差，
风控层另有价格 sanity 双保险）。

字节流必须按 GBK 解码（Loop #12 经验：双重转码会丢中文名）。
"""
import re
from dataclasses import dataclass
from typing import Dict, Optional

import requests

from .broker_base import limit_pct, to_tencent

PROXIES = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
TIMEOUT = 8


@dataclass
class Quote:
    code: str          # 6 位数字
    name: str
    last: float        # 最新价
    prev_close: float
    limit_up: float
    limit_down: float
    suspended: bool = False   # 停牌（最新价=0）


def _fetch_raw(tcodes: list) -> str:
    url = "https://qt.gtimg.cn/q=" + ",".join(tcodes)
    last_err = None
    for use_proxy in (False, True):
        try:
            r = requests.get(url, timeout=TIMEOUT,
                             proxies=PROXIES if use_proxy else None)
            return r.content.decode("gbk", errors="replace")
        except Exception as e:
            last_err = e
    raise RuntimeError(f"腾讯行情请求失败: {last_err}")


def quotes(codes: list) -> Dict[str, Quote]:
    """批量取行情，key 为 6 位代码。单只失败不中断，缺失的代码不在返回 dict 中。"""
    if not codes:
        return {}
    tcodes = sorted({to_tencent(c) for c in codes})
    raw = _fetch_raw(tcodes)
    out: Dict[str, Quote] = {}
    for line in raw.split(";"):
        m = re.match(r'\s*v_(\w+)="([^"]*)"', line.strip())
        if not m:
            continue
        tcode, payload = m.groups()
        parts = payload.split("~")
        if len(parts) < 40 or not parts[3]:
            continue
        num = tcode[2:]
        name = parts[1]
        try:
            last = float(parts[3])
            prev = float(parts[4])
        except ValueError:
            continue
        pct = limit_pct(num, name)
        out[num] = Quote(
            code=num, name=name, last=last, prev_close=prev,
            limit_up=round(prev * (1 + pct), 2),
            limit_down=round(prev * (1 - pct), 2),
            suspended=(last == 0),
        )
    return out


def quote(code: str) -> Optional[Quote]:
    return quotes([code]).get(code.split(".")[0])
