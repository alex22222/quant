#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""晨会数据包生成器（确定性代码，LLM 团队的「分析师原材料」）

汇总 watchlist 技术指标、paper 账户状态、市场状态、近期新闻头条，
输出 trading_team/briefing/YYYY-MM-DD.md 供晨会 Automation 使用。

用法: python3 trading_team/briefing.py [--date YYYY-MM-DD]
"""
import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "kde_levels"))
from levels import BundleData  # noqa: E402

WATCHLIST = ROOT / "trading_team" / "watchlist.json"
OUT_DIR = ROOT / "trading_team" / "briefing"
PAPER_DB = ROOT / "paper" / "paper.db"
NEWS_DIR = ROOT / "console" / "news"


def to_rq(code: str, secid: str) -> str:
    """watchlist 的 600519/1.600519 -> 600519.XSHG"""
    return f"{code}.XSHG" if secid.startswith("1.") else f"{code}.XSHE"


def tech_row(bd, rq_code):
    df = bd.load(rq_code)
    c = df["close"].values
    if len(c) < 125:
        return None
    import numpy as np
    last = float(c[-1])
    ma20, ma60, ma120 = (float(c[-n:].mean()) for n in (20, 60, 120))
    mom20 = last / float(c[-21]) - 1
    ret = np.diff(c[-61:]) / c[-61:-1]
    vol20 = float(ret[-20:].std() * (252 ** 0.5))
    hi52, lo52 = float(c[-250:].max() if len(c) >= 250 else max(c)), \
                 float(c[-250:].min() if len(c) >= 250 else min(c))
    pos52 = (last - lo52) / (hi52 - lo52) if hi52 > lo52 else 0.5
    return {"close": last, "ma20": ma20, "ma60": ma60, "ma120": ma120,
            "mom20": mom20, "vol20": vol20, "pos52": pos52,
            "trend": "多头" if last > ma20 > ma60 else ("空头" if last < ma20 < ma60 else "震荡")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()
    day = args.date

    wl = json.load(open(WATCHLIST, encoding="utf-8"))
    bd = BundleData()

    # 市场状态（沪深300 vs 120 日线）
    bench = wl["benchmark"]
    idx = bd.load_index(f"{bench['code']}.XSHG")
    ic = idx["close"].values
    market_ok = bool(ic[-1] > ic[-120:].mean())
    latest_day = idx.index[-1].date().isoformat()
    idx_line = (f"{bench['name']} 收盘 {ic[-1]:.2f}，"
                f"{'位于' if market_ok else '跌破'} 120 日均线（{ic[-120:].mean():.2f}）"
                f" → {'允许开仓' if market_ok else '风控：空仓观望'}")

    # watchlist 技术表
    rows = []
    for s in wl["stocks"]:
        t = tech_row(bd, to_rq(s["code"], s["secid"]))
        if t:
            rows.append((s, t))
    rows.sort(key=lambda x: -x[1]["mom20"])

    # paper 账户
    paper_lines = []
    if PAPER_DB.exists():
        conn = sqlite3.connect(PAPER_DB)
        acc = conn.execute("SELECT day, cash, equity, note FROM account ORDER BY rowid DESC LIMIT 1").fetchone()
        pos = conn.execute("SELECT code, qty, avg_cost FROM positions").fetchall()
        conn.close()
        if acc:
            paper_lines.append(f"- 净值 ¥{acc[2]:,.0f} · 现金 ¥{acc[1]:,.0f} · 记账日 {acc[0]}（{acc[3]}）")
        paper_lines.append("- 持仓: " + ("、".join(f"{c} {q:.0f}股" for c, q, _ in pos) if pos else "空仓"))

    # 近 3 天新闻头条
    news_lines = []
    idx_file = NEWS_DIR / "index.json"
    if idx_file.exists():
        for n in json.load(open(idx_file, encoding="utf-8"))[:3]:
            news_lines.append(f"- {n['date']}（{n['count']} 条）: {n.get('headline', '')}")

    tbl = ["| 排名 | 股票 | 板块 | 收盘 | 20日动量 | 趋势 | 收盘vs MA20 | 年化波动 | 52周位置 |",
           "|---|---|---|---|---|---|---|---|---|"]
    for i, (s, t) in enumerate(rows, 1):
        tbl.append(f"| {i} | {s['name']} {s['code']} | {s['sector']} | {t['close']:.2f} | "
                   f"{t['mom20']*100:+.1f}% | {t['trend']} | {(t['close']/t['ma20']-1)*100:+.1f}% | "
                   f"{t['vol20']*100:.0f}% | {t['pos52']*100:.0f}% |")

    md = f"""# 晨会数据包 · {day}

> 生成时间 {datetime.now():%Y-%m-%d %H:%M} · 数据截至 {latest_day}（本地 bundle）
> 本包由确定性代码生成，是 LLM 团队（分析师→研究员→交易员→风控）的唯一数据入口

## 市场状态（风控前置）
{idx_line}

## 观察池技术快照（按 20 日动量排序）
{chr(10).join(tbl)}

## Paper 账户现状
{chr(10).join(paper_lines) or '- （无台账）'}

## 近期量化情报（quant-x-monitor 摘要）
{chr(10).join(news_lines) or '- （无）'}

## 使用说明
- 分析师 01-04 基于本包 + 各自检索的公开信息出报告
- 研究员 05 组织多空辩论；交易员 06 出计划；风控 07 出裁决
- 市场状态为「风控：空仓观望」时，交易员只能给出观望或减仓计划
"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{day}.md"
    out.write_text(md, encoding="utf-8")
    print(f"[Briefing] {out} （{len(rows)} 只股票，数据截至 {latest_day}）")


if __name__ == "__main__":
    main()
