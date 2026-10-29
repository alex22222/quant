#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OKX 日线数据抓取（绕过 ccxt 代理问题，直连 REST）

选币原则：高流动性主流币 + 有 ≥400 天历史（新秀币自动跳过）。
输出 crypto_admission/data/{BASE}.csv（date, open, high, low, close, volume）
"""
import json
import time
from pathlib import Path

import pandas as pd
import requests

PROXY = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
BASE_URL = "https://www.okx.com"
OUT = Path(__file__).resolve().parent / "data"
OUT.mkdir(exist_ok=True)

# 候选池：主流 + 各赛道龙头（取交集：存在且历史够长）
CANDIDATES = [
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "LTC",
    "BCH", "UNI", "ATOM", "NEAR", "FIL", "APT", "ARB", "OP", "INJ", "SUI",
    "STX", "FET", "GRT", "AAVE", "LDO", "CRV", "SNX", "TRX", "ETC",
    "XLM", "ALGO", "PEPE", "SHIB", "IMX", "MANA", "SAND", "AXS", "COMP", "ENS",
]
DAYS = 800          # 目标历史长度
MIN_DAYS = 400      # 不足则剔除


def fetch_candles(inst: str, after_ms: int = None) -> list:
    """history-candles 每次最多 100 条（1Dutc）。OKX 翻页语义：
    after = 返回早于该时间戳的记录（往历史翻）；before = 晚于（往最新翻）。"""
    params = {"instId": inst, "bar": "1Dutc", "limit": "100"}
    if after_ms:
        params["after"] = str(after_ms)
    r = requests.get(f"{BASE_URL}/api/v5/market/history-candles",
                     params=params, proxies=PROXY, timeout=30)
    data = r.json()
    if data.get("code") != "0":
        raise RuntimeError(f"{inst}: {data.get('msg')}")
    return data["data"]


def main():
    ok, skip = [], []
    for base in CANDIDATES:
        inst = f"{base}-USDT"
        path = OUT / f"{base}.csv"
        if path.exists():
            df = pd.read_csv(path)
            (ok if len(df) >= MIN_DAYS else skip).append(base)
            continue
        try:
            rows, after = [], None
            while len(rows) < DAYS:
                batch = fetch_candles(inst, after)
                if not batch:
                    break
                rows.extend(batch)
                after = int(batch[-1][0])       # 最旧一根的时间戳，继续往历史翻
                time.sleep(0.12)                # 限速 20 req/s 档位留余量
            if len(rows) < MIN_DAYS:
                skip.append(base)
                print(f"[skip] {base}: 仅 {len(rows)} 天")
                continue
            df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close",
                                             "vol", "volCcy", "volCcyQuote", "confirm"])
            df["date"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms").dt.date
            df = df[["date", "open", "high", "low", "close", "volCcyQuote"]]
            df.columns = ["date", "open", "high", "low", "close", "amount"]
            df = df.astype({c: float for c in ["open", "high", "low", "close", "amount"]})
            df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
            df.to_csv(path, index=False)
            ok.append(base)
            print(f"[ok] {base}: {len(df)} 天  {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")
        except Exception as e:
            skip.append(base)
            print(f"[err] {base}: {type(e).__name__} {str(e)[:80]}")
        time.sleep(0.2)
    (Path(__file__).parent / "universe.json").write_text(
        json.dumps({"ok": ok, "skip": skip}, ensure_ascii=False, indent=1))
    print(f"\n宇宙: {len(ok)} 只 -> {ok}")


if __name__ == "__main__":
    main()
