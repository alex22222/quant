#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Yahoo Finance 美股日线抓取（curl 子进程版 —— python requests 会被指纹限流，curl 正常）"""
import json
import subprocess
import time
from io import StringIO
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent / "data"
OUT.mkdir(exist_ok=True)
UA = "Mozilla/5.0"   # Yahoo 对长 UA 指纹限流（429），短 UA 正常

SYMS = ["SPY", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "TSLA",
        "AMD", "NFLX", "CRM", "ORCL", "ADBE", "QCOM", "TXN", "INTC", "CSCO",
        "JPM", "BAC", "GS", "MS", "V", "MA", "PYPL", "XOM", "CVX", "WMT",
        "COST", "HD", "NKE", "MCD", "DIS", "PFE", "UNH", "JNJ", "CAT", "BA"]
MIN_DAYS = 700


def curl_json(sym: str, retries: int = 8) -> dict:
    url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?range=5y&interval=1d&events=div,splits")
    for attempt in range(retries):
        r = subprocess.run(
            ["curl", "-s", "--max-time", "30", "-x", "http://127.0.0.1:7890",
             "-H", f"User-Agent: {UA}", url],
            capture_output=True, text=True)
        txt = r.stdout.strip()
        if txt.startswith("{"):
            return json.loads(txt)
        print(f"  [{sym}] 429/失败，{15*(attempt+1)}s 后重试 ({attempt+1}/{retries})", flush=True)
        time.sleep(15 * (attempt + 1))       # 429 退避
    raise RuntimeError(f"{sym}: 重试 {retries} 次仍失败")


def to_df(j: dict) -> pd.DataFrame:
    res = j["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({"date": pd.to_datetime(ts, unit="s").date,
                       "open": q["open"], "high": q["high"], "low": q["low"],
                       "close": q["close"], "vol": q["volume"]})
    df = df.dropna().drop_duplicates("date").sort_values("date").reset_index(drop=True)
    df["amount"] = df["close"] * df["vol"]
    return df[["date", "open", "high", "low", "close", "amount"]]


def main():
    ok, fail = [], []
    for s in SYMS:
        path = OUT / f"{s}.csv"
        if path.exists() and len(pd.read_csv(path)) >= MIN_DAYS:
            ok.append(s)
            continue
        try:
            df = to_df(curl_json(s))
            if len(df) < MIN_DAYS:
                fail.append(s)
                print(f"[short] {s}: {len(df)} 天", flush=True)
                continue
            df.to_csv(path, index=False)
            ok.append(s)
            print(f"[ok] {s}: {len(df)} 天 {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}", flush=True)
        except Exception as e:
            fail.append(s)
            print(f"[err] {s}: {type(e).__name__} {str(e)[:80]}", flush=True)
        time.sleep(2)
    (Path(__file__).parent / "universe.json").write_text(
        json.dumps({"ok": ok, "fail": fail}, indent=1))
    print(f"\n宇宙: {len(ok)} 只（含基准 SPY），失败 {fail}")


if __name__ == "__main__":
    main()
