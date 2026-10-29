"""Trading Team 数据采集层 — 确定性采集，供 LLM 角色分析。

用法:  .venv/bin/python -m trading_team.collect [--date YYYY-MM-DD]

产出:  trading_team/context/<date>/
  ├── manifest.json            采集健康清单（哪些成功/失败、数据时间戳）
  ├── snapshot.json            观察池 + 基准 实时/收盘快照
  ├── technical_<code>.json    120 日 K 线 + MA/MACD/RSI/量比/高低点
  ├── fundamental_<code>.json  估值 + 主要财务指标 + 公告
  ├── news.json                财经快讯 + 公司公告汇总
  └── sentiment.json           涨跌停池、市场宽度、个股情绪指标

纪律: 单源失败不中断整体；失败记入 manifest（Loop 工程：数据健康可见）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from datetime import date, datetime, time as clock_time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import requests

from trading_team.run_ledger import RunLedger
from trading_team.state_lock import state_lock

ROOT = Path(__file__).resolve().parent
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
           "Referer": "https://quote.eastmoney.com/"}
TIMEOUT = 12
COLLECTOR_SCHEMA_VERSION = 7
DEFAULT_TRADING_CALENDAR = Path.home() / ".rqalpha" / "bundle" / "trading_dates.npy"


def _get(url: str, params: dict | None = None, retries: int = 3) -> dict:
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            break
        except Exception as e:  # 网络/代理抖动，退避重试
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    else:
        raise last_err  # type: ignore[misc]
    text = r.text.strip()
    # 部分接口返回 JSONP
    if text.startswith("jQuery") or (text and not text.startswith(("{", "["))):
        l, rr = text.find("("), text.rfind(")")
        if l != -1 and rr != -1:
            text = text[l + 1:rr]
    return json.loads(text)


def load_watchlist(root: Path = ROOT) -> dict:
    return json.loads((root / "watchlist.json").read_text(encoding="utf-8"))


# ---------- 行情快照 ----------
def _secid_to_tx(secid: str) -> str:
    """1.600519 -> sh600519, 0.000063 -> sz000063"""
    mkt, code = secid.split(".")
    return ("sh" if mkt == "1" else "sz") + code


def fetch_snapshot_tx(secids: list[str]) -> dict:
    """腾讯行情快照（兜底源）。"""
    r = requests.get("https://qt.gtimg.cn/q=" + ",".join(_secid_to_tx(s) for s in secids),
                     headers={**HEADERS, "Referer": "https://gu.qq.com/"}, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = "gbk"
    out = {}
    for m in __import__("re").finditer(r'v_(\w+)="([^"]*)"', r.text):
        f = m.group(2).split("~")
        if len(f) < 50:
            continue
        def num(i, scale=1.0):
            try:
                return round(float(f[i]) * scale, 2) if f[i] else None
            except (ValueError, IndexError):
                return None
        out[f[2]] = {
            "name": f[1], "close": num(3), "prev_close": num(4), "open": num(5),
            "change": num(31), "pct_chg": num(32), "high": num(33), "low": num(34),
            "volume": num(36), "amount": num(37, 1e4), "turnover_rate": num(38),
            "pe_ttm": num(39), "amplitude": num(43),
            "float_mktcap": num(44, 1e8), "total_mktcap": num(45, 1e8),
            "pb": num(46), "volume_ratio": num(49),
            "quote_time": f[30], "price_basis": "unadjusted",
            "limit_up": num(47), "limit_down": num(48),
        }
    return out


def fetch_snapshot(secids: list[str]) -> dict:
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    fields = "f2,f3,f4,f5,f6,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21,f23,f115,f152"
    data = _get(url, {"secids": ",".join(secids), "fields": fields, "fltt": 2, "invt": 2})
    out = {}
    for item in (data.get("data") or {}).get("diff") or []:
        def num(k):
            v = item.get(k)
            return None if v in (None, "-") else v
        out[item["f12"]] = {
            "name": item.get("f14"), "close": num("f2"), "pct_chg": num("f3"),
            "change": num("f4"), "volume": num("f5"), "amount": num("f6"),
            "turnover_rate": num("f8"), "pe_dynamic": num("f9"), "volume_ratio": num("f10"),
            "high": num("f15"), "low": num("f16"), "open": num("f17"), "prev_close": num("f18"),
            "total_mktcap": num("f20"), "float_mktcap": num("f21"), "pb": num("f23"),
            "pe_ttm": num("f115"),
        }
    return out


# ---------- K线 ----------
def fetch_kline_tx(secid: str, limit: int = 160) -> list[dict]:
    """腾讯日 K（前复权，兜底源）。字段: date,open,close,high,low,volume(手)"""
    r = requests.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                     params={"param": f"{_secid_to_tx(secid)},day,,,{limit},qfq"},
                     headers={**HEADERS, "Referer": "https://gu.qq.com/"}, timeout=TIMEOUT)
    d = r.json()["data"][_secid_to_tx(secid)]
    kl = d.get("qfqday") or d.get("day") or []
    rows = []
    prev = None
    for p in kl:
        o, c, hi, lo, v = float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])
        rows.append({
            "date": p[0], "open": o, "close": c, "high": hi, "low": lo, "volume": v,
            "amount": None,
            "amplitude": round((hi - lo) / prev * 100, 2) if prev else None,
            "pct_chg": round((c / prev - 1) * 100, 2) if prev else None,
            "change": round(c - prev, 2) if prev else None,
            "turnover": None,
        })
        prev = c
    return rows


def fetch_kline(secid: str, limit: int = 160) -> list[dict]:
    """东财主源，腾讯兜底。"""
    try:
        rows = _fetch_kline_em(secid, limit)
        if rows:
            return rows
    except Exception:
        pass
    return fetch_kline_tx(secid, limit)


def _fetch_kline_em(secid: str, limit: int = 160) -> list[dict]:
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    data = _get(url, {
        "secid": secid, "klt": 101, "fqt": 1, "lmt": limit, "end": 20500101,
        "fields1": "f1,f2,f3,f7", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    })
    kl = ((data.get("data") or {}).get("klines")) or []
    rows = []
    for line in kl:
        p = line.split(",")
        rows.append({
            "date": p[0], "open": float(p[1]), "close": float(p[2]),
            "high": float(p[3]), "low": float(p[4]), "volume": float(p[5]),
            "amount": float(p[6]), "amplitude": float(p[7]),
            "pct_chg": float(p[8]), "change": float(p[9]), "turnover": float(p[10]),
        })
    return rows


# ---------- 技术指标 ----------
def compute_indicators(rows: list[dict]) -> dict:
    closes = [r["close"] for r in rows]
    vols = [r["volume"] for r in rows]

    def ma(vals, n):
        return round(sum(vals[-n:]) / n, 2) if len(vals) >= n else None

    def ema_series(vals, n):
        k = 2 / (n + 1)
        e = vals[0]
        out = [e]
        for v in vals[1:]:
            e = v * k + e * (1 - k)
            out.append(e)
        return out

    ema12, ema26 = ema_series(closes, 12), ema_series(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = ema_series(dif, 9)
    macd_bar = [2 * (d - e) for d, e in zip(dif, dea)]

    # RSI14 (Wilder)
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0)); losses.append(max(-ch, 0))
    rsi14 = None
    if len(gains) >= 14:
        ag = sum(gains[:14]) / 14; al = sum(losses[:14]) / 14
        for g, l in zip(gains[14:], losses[14:]):
            ag = (ag * 13 + g) / 14; al = (al * 13 + l) / 14
        rsi14 = round(100 - 100 / (1 + ag / al), 2) if al > 0 else 100.0

    # Stochastic Oscillator（14/3）：%K=(C-LL14)/(HH14-LL14)，%D=%K 的 3 日均值
    def k_at(i):
        win = rows[i - 13:i + 1]
        hh = max(r["high"] for r in win); ll = min(r["low"] for r in win)
        return (rows[i]["close"] - ll) / (hh - ll) * 100 if hh > ll else 50.0

    stoch_k = stoch_d = None
    if len(rows) >= 16:
        ks = [k_at(i) for i in range(len(rows) - 3, len(rows))]
        stoch_k, stoch_d = round(ks[-1], 2), round(sum(ks) / 3, 2)

    # VWAP（日线近似：20 日典型价×成交量加权）
    vwap_20d = None
    if len(rows) >= 20:
        win = rows[-20:]
        pv = sum((r["high"] + r["low"] + r["close"]) / 3 * r["volume"] for r in win)
        vv = sum(r["volume"] for r in win)
        vwap_20d = round(pv / vv, 2) if vv else None

    # 布林带（20, 2）
    boll = {"mid": None, "upper": None, "lower": None, "pct_b": None}
    if len(closes) >= 20:
        win = closes[-20:]
        mid = sum(win) / 20
        sd = (sum((x - mid) ** 2 for x in win) / 20) ** 0.5
        upper, lower = mid + 2 * sd, mid - 2 * sd
        boll = {"mid": round(mid, 2), "upper": round(upper, 2), "lower": round(lower, 2),
                "pct_b": round((closes[-1] - lower) / (upper - lower), 3) if upper > lower else None}

    last = rows[-1]
    recent = rows[-60:]
    return {
        "as_of": last["date"],
        "close": last["close"], "pct_chg": last.get("pct_chg"), "turnover": last.get("turnover"),
        "ma5": ma(closes, 5), "ma10": ma(closes, 10), "ma20": ma(closes, 20), "ma60": ma(closes, 60),
        "dif": round(dif[-1], 3), "dea": round(dea[-1], 3), "macd_bar": round(macd_bar[-1], 3),
        "macd_bar_prev": round(macd_bar[-2], 3),
        "rsi14": rsi14,
        "stoch_k": stoch_k, "stoch_d": stoch_d,
        "vwap_20d": vwap_20d, "bollinger": boll,
        "vol_ma5": ma(vols, 5), "vol_ma20": ma(vols, 20),
        "vol_ratio_vs20": round(vols[-1] / (sum(vols[-21:-1]) / 20), 2) if len(vols) > 20 else None,
        "high_20d": max(r["high"] for r in rows[-20:]),
        "low_20d": min(r["low"] for r in rows[-20:]),
        "high_60d": max(r["high"] for r in recent),
        "low_60d": min(r["low"] for r in recent),
        "ret_5d": round((closes[-1] / closes[-6] - 1) * 100, 2) if len(closes) > 5 else None,
        "ret_20d": round((closes[-1] / closes[-21] - 1) * 100, 2) if len(closes) > 20 else None,
        "ret_60d": round((closes[-1] / closes[-61] - 1) * 100, 2) if len(closes) > 60 else None,
    }


# ---------- 基本面 ----------
def fetch_financials(code: str, secid: str) -> dict:
    mkt = "SH" if secid.startswith("1.") else "SZ"
    secucode = f"{code}.{mkt}"
    url = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
    data = _get(url, {
        "reportName": "RPT_F10_FINANCE_MAINFINADATA",
        "columns": "SECUCODE,REPORT_DATE,EPSJB,TOTALOPERATEREVE,PARENTNETPROFIT,KCFJCXSYJLR,"
                   "TOTALOPERATEREVETZ,PARENTNETPROFITTZ,KCFJCXSYJLRTZ,ROEJQ,XSMLL,XSJLL,ZCFZL",
        "filter": f'(SECUCODE="{secucode}")',
        "pageNumber": 1, "pageSize": 4, "sortTypes": -1, "sortColumns": "REPORT_DATE",
        "source": "HSF10", "client": "PC",
    })
    rows = (data.get("result") or {}).get("data") or []
    reports = []
    for r in rows:
        reports.append({
            "report_date": (r.get("REPORT_DATE") or "")[:10],
            "eps": r.get("EPSJB"),
            "revenue_yi": _yi(r.get("TOTALOPERATEREVE")),
            "net_profit_yi": _yi(r.get("PARENTNETPROFIT")),
            "deducted_profit_yi": _yi(r.get("KCFJCXSYJLR")),
            "revenue_yoy": r.get("TOTALOPERATEREVETZ"),
            "profit_yoy": r.get("PARENTNETPROFITTZ"),
            "deducted_yoy": r.get("KCFJCXSYJLRTZ"),
            "roe_weighted": r.get("ROEJQ"),
            "gross_margin": r.get("XSMLL"),
            "net_margin": r.get("XSJLL"),
            "debt_ratio": r.get("ZCFZL"),
        })
    return {"reports": reports}


def _yi(v):
    return round(v / 1e8, 2) if isinstance(v, (int, float)) else v


def fetch_announcements(code: str, limit: int = 8) -> list[dict]:
    url = "https://np-anotice-stock.eastmoney.com/api/security/ann"
    data = _get(url, {
        "sr": -1, "page_size": limit, "page_index": 1, "ann_type": "A",
        "client_source": "web", "stock_list": code, "f_node": 0, "s_node": 0,
    })
    out = []
    for a in (data.get("data") or {}).get("list") or []:
        out.append({
            "date": (a.get("notice_date") or "")[:10],
            "title": a.get("title"),
            "columns": [c.get("column_name") for c in a.get("columns") or []],
        })
    return out


# ---------- 新闻 ----------
def fetch_fastnews(limit: int = 30) -> list[dict]:
    url = "https://np-weblist.eastmoney.com/comm/web/getFastNews"
    data = _get(url, {"client": "web", "biz": "web_724", "fastColumn": "102",
                      "pageSize": limit, "sortEnd": "", "req_trace": str(int(time.time() * 1000))})
    payload = data.get("data") or {}
    news_list = payload.get("fastNewsList") if isinstance(payload, dict) else payload
    out = []
    for n in news_list or []:
        out.append({"time": n.get("showTime"), "title": n.get("title"),
                    "digest": (n.get("digest") or "")[:200]})
    return out


# ---------- 情绪 ----------
def fetch_zt_pool(trade_date: str) -> dict:
    ymd = trade_date.replace("-", "")
    url = "https://push2ex.eastmoney.com/getTopicZTPool"
    data = _get(url, {
        "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
        "Pageindex": 0, "pagesize": 500, "sort": "fbt:asc", "date": ymd,
    })
    pool = (data.get("data") or {}).get("pool") or []
    return {"date": trade_date, "zt_count": len(pool),
            "samples": [{"name": p.get("n"), "code": p.get("c"),
                         "reason": p.get("hybk")} for p in pool[:15]]}


def fetch_dt_pool(trade_date: str) -> dict:
    ymd = trade_date.replace("-", "")
    url = "https://push2ex.eastmoney.com/getTopicDTPool"
    data = _get(url, {
        "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
        "Pageindex": 0, "pagesize": 500, "sort": "fund:asc", "date": ymd,
    })
    pool = (data.get("data") or {}).get("pool") or []
    return {"date": trade_date, "dt_count": len(pool)}


def _fetch_market_breadth_sina_tx(trade_date: str) -> dict:
    """Sina defines the universe; only dated Tencent quotes determine breadth."""
    base = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    pages = []

    def sina(method, params):
        response = requests.get(base + method, params=params, timeout=TIMEOUT,
                                headers={**HEADERS, "Referer": "https://finance.sina.com.cn/"})
        response.raise_for_status()
        value = response.json()
        pages.append({"source": base + method, "params": params, "response": value})
        return value

    def universe_count():
        value = sina("Market_Center.getHQNodeStockCount", {"node": "hs_a"})
        if ((type(value) is not int and not (isinstance(value, str) and value.isascii() and value.isdigit()))
                or not 0 < int(value) <= 20000):
            raise ValueError("breadth universe count invalid")
        return int(value)

    total = universe_count()
    seen, symbols, excluded = set(), [], 0
    for page in range(1, math.ceil(total / 80) + 1):
        rows = sina("Market_Center.getHQNodeData", {
            "page": page, "num": 80, "sort": "symbol", "asc": 1,
            "node": "hs_a", "symbol": "", "_s_r_a": "page",
        })
        if not isinstance(rows, list) or len(rows) != min(80, total - len(seen)):
            raise ValueError("breadth universe incomplete page")
        for row in rows:
            symbol = row.get("symbol") if isinstance(row, dict) else None
            if (not isinstance(symbol, str) or not re.fullmatch(r"(?:sh|sz|bj)[0-9]{6}", symbol)
                    or row.get("code") != symbol[2:] or symbol in seen):
                raise ValueError("breadth universe invalid or duplicate symbol")
            seen.add(symbol)
            if symbol.startswith("bj"):
                excluded += 1
            elif re.fullmatch(r"(?:sh6|sz[03])[0-9]{5}", symbol):
                symbols.append(symbol)
            else:
                raise ValueError(f"breadth unexpected universe member: {symbol}")
        time.sleep(0.2)
    if universe_count() != total or len(seen) != total or not symbols:
        raise ValueError("breadth universe changed or empty")

    counts = dict.fromkeys(("up_gt5", "up", "down", "down_gt5", "flat", "unavailable"), 0)
    symbols.sort()
    quote_url = "https://qt.gtimg.cn/q="
    for offset in range(0, len(symbols), 80):
        batch = symbols[offset:offset + 80]
        response = requests.get(quote_url + ",".join(batch), timeout=TIMEOUT,
                                headers={**HEADERS, "Referer": "https://gu.qq.com/"})
        response.raise_for_status()
        raw = response.content
        text = raw.decode("gbk")
        found = set()
        for line in text.splitlines():
            if not line.strip():
                continue
            match = re.fullmatch(r'v_((?:sh|sz)[0-9]{6})="([^"]*)";', line.strip())
            if not match:
                raise ValueError("breadth malformed Tencent quote")
            symbol, value = match.groups()
            fields = value.split("~")
            if symbol not in batch or symbol in found or len(fields) < 50 or fields[2] != symbol[2:]:
                raise ValueError("breadth invalid or duplicate quote identity")
            found.add(symbol)
            if not re.fullmatch(r"[0-9]{14}", fields[30]):
                raise ValueError(f"breadth invalid quote timestamp: {symbol}")
            stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S")
            if stamp.date().isoformat() != trade_date or stamp.time() < clock_time(15):
                raise ValueError(f"breadth quote session differs from completed {trade_date}: {symbol}")
            close, prev, change = (Decimal(fields[index]) for index in (3, 4, 32))
            if not all(v.is_finite() for v in (close, prev, change)) or close <= 0 or prev <= 0:
                raise ValueError(f"breadth unavailable quote: {symbol}")
            raw_change = (close / prev - 1) * 100
            if raw_change.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != change:
                raise ValueError(f"breadth inconsistent quote change: {symbol}")
            counts["up"] += raw_change > 0
            counts["down"] += raw_change < 0
            counts["flat"] += raw_change == 0
            counts["up_gt5"] += raw_change > 5
            counts["down_gt5"] += raw_change < -5
        if found != set(batch):
            raise ValueError("breadth missing Tencent quotes")
        pages.append({"source": quote_url, "symbols": batch, "encoding": "gbk",
                      "response": text, "sha256": hashlib.sha256(raw).hexdigest()})
        time.sleep(0.2)
    return {**counts, "schema": "market_breadth.v1", "date": trade_date, "total": len(symbols),
            "source": quote_url, "universe_source": base + "Market_Center.getHQNodeData",
            "universe": "Sina hs_a SH/SZ main board, ChiNext and STAR; excludes Beijing; provider-defined coverage",
            "universe_total": total, "excluded_beijing": excluded,
            "fetched_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            "pages": pages}


def fetch_market_breadth(trade_date: str) -> dict:
    """Count the complete SH/SZ equity snapshot, retaining raw page evidence."""
    date.fromisoformat(trade_date)
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    counts = dict.fromkeys(("up_gt5", "up", "down", "down_gt5", "flat", "unavailable"), 0)
    pages, seen, total = [], set(), None
    while total is None or len(seen) < total:
        # The endpoint ignores filter= expressions; sort by code, not changing returns.
        params = {"pn": len(pages) + 1, "pz": 100, "po": 0, "np": 1,
                  "fltt": 2, "invt": 2, "fid": "f12",
                  "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                  "fields": "f3,f12,f13,f124"}
        try:
            response = _get(url, params)
        except requests.RequestException as primary_error:
            # Invalid primary data must still fail closed; only transport failure switches source.
            try:
                fallback = _fetch_market_breadth_sina_tx(trade_date)
            except (requests.RequestException, ValueError, InvalidOperation) as fallback_error:
                raise ValueError(f"breadth primary failed ({primary_error}); fallback failed "
                                 f"({type(fallback_error).__name__}: {fallback_error})") from fallback_error
            fallback["primary_failure"] = {"source": url, "params": params,
                                           "type": type(primary_error).__name__, "detail": str(primary_error)}
            fallback["pages"] = [{"source": url, **page} for page in pages] + fallback["pages"]
            return fallback
        data = response.get("data")
        if response.get("rc") != 0 or not isinstance(data, dict):
            raise ValueError("breadth source payload unavailable")
        reported_total, rows = data.get("total"), data.get("diff")
        if (type(reported_total) is not int or not 0 < reported_total <= 20000
                or (total is not None and reported_total != total)):
            raise ValueError("breadth total invalid or changed during pagination")
        total = reported_total
        if not isinstance(rows, list) or len(rows) != min(100, total - len(seen)):
            raise ValueError("breadth incomplete page")
        for row in rows:
            code, market = row.get("f12"), row.get("f13")
            if (not isinstance(code, str) or len(code) != 6 or not code.isdigit()
                    or type(market) is not int or market not in (0, 1)):
                raise ValueError("breadth symbol identity invalid")
            symbol = (market, code)
            if symbol in seen:
                raise ValueError("breadth duplicate symbol during pagination")
            seen.add(symbol)
            change = row.get("f3")
            if change in (None, "-"):
                counts["unavailable"] += 1
                continue
            if (isinstance(change, bool) or not isinstance(change, (int, float))
                    or not math.isfinite(change)):
                raise ValueError("breadth nonnumeric change")
            stamp = row.get("f124")
            if (type(stamp) is not int or stamp <= 0
                    or datetime.fromtimestamp(stamp, ZoneInfo("Asia/Shanghai")).date().isoformat() != trade_date):
                raise ValueError(f"breadth quote session differs from {trade_date}: {code}")
            counts["up"] += change > 0
            counts["down"] += change < 0
            counts["flat"] += change == 0
            counts["up_gt5"] += change > 5
            counts["down_gt5"] += change < -5
        pages.append({"params": params, "response": response})
    if counts["unavailable"] == total:
        raise ValueError("breadth has no usable quotes")
    return {**counts, "schema": "market_breadth.v1", "date": trade_date, "total": total,
            "source": url, "universe": "SH/SZ main board, ChiNext and STAR; excludes Beijing",
            "fetched_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            "pages": pages}


# ---------- 主流程 ----------
def _hash_json(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_trading_days(path: Path = DEFAULT_TRADING_CALENDAR) -> set[str]:
    raw = np.load(path, allow_pickle=False)
    return {
        f"{int(value) // 10000:04d}-{int(value) // 100 % 100:02d}-{int(value) % 100:02d}"
        for value in raw
    }


def validate_completed_session(
    requested_day: str,
    technical_dates: dict[str, str],
    expected_codes: set[str] | None = None,
    trading_days: set[str] | None = None,
    now: datetime | None = None,
) -> dict:
    actual_codes = set(technical_dates)
    if expected_codes is not None and actual_codes != expected_codes:
        missing = sorted(expected_codes - actual_codes)
        unexpected = sorted(actual_codes - expected_codes)
        details = []
        if missing:
            details.append(f"缺少 {missing}")
        if unexpected:
            details.append(f"多出 {unexpected}")
        return {
            "ok": False,
            "latest_trade_date": None,
            "detail": f"技术数据标的不完整: {', '.join(details)}",
        }
    if not technical_dates:
        return {"ok": False, "latest_trade_date": None, "detail": "无有效技术数据日期"}
    unique = sorted(set(technical_dates.values()))
    if len(unique) != 1:
        return {
            "ok": False,
            "latest_trade_date": None,
            "detail": f"技术数据日期不一致: {technical_dates}",
        }
    latest = date.fromisoformat(unique[0])
    requested = date.fromisoformat(requested_day)
    if latest > requested:
        return {
            "ok": False,
            "latest_trade_date": latest.isoformat(),
            "detail": f"数据日期 {latest} 晚于运行日期 {requested}",
        }
    if trading_days is not None and not trading_days:
        return {
            "ok": False,
            "latest_trade_date": latest.isoformat(),
            "detail": "A 股交易日历不可用，拒绝确认完成会话",
        }
    if trading_days is not None and latest.isoformat() not in trading_days:
        return {
            "ok": False,
            "latest_trade_date": latest.isoformat(),
            "detail": f"数据日期 {latest} 不在 A 股交易日历",
        }
    if trading_days is None and latest.weekday() >= 5:
        return {
            "ok": False,
            "latest_trade_date": latest.isoformat(),
            "detail": f"数据日期 {latest} 不是交易日",
        }
    now = now or datetime.now()
    if latest == now.date() and now.time() < clock_time(15, 10):
        return {
            "ok": False,
            "latest_trade_date": latest.isoformat(),
            "detail": f"{latest} 尚未完成收盘，拒绝日内不完整 K 线",
        }
    return {
        "ok": True,
        "latest_trade_date": latest.isoformat(),
        "detail": f"{len(technical_dates)} 个标的日期一致: {latest}",
    }


def collect_day(
    day: str,
    *,
    root: Path = ROOT,
    ledger: RunLedger | None = None,
    now: datetime | None = None,
    calendar_path: Path = DEFAULT_TRADING_CALENDAR,
) -> dict:
    with state_lock(root, f"data-collect-{day}"):
        return _collect_day(day, root=root, ledger=ledger, now=now, calendar_path=calendar_path)


def _collect_day(day: str, *, root: Path, ledger: RunLedger | None,
                 now: datetime | None, calendar_path: Path) -> dict:
    from trading_team.market_archive import capture_closing, completed_day

    date.fromisoformat(day)
    out_dir = root / "context" / day
    out_dir.mkdir(parents=True, exist_ok=True)
    wl = load_watchlist(root)
    stocks = wl["stocks"]
    bench = wl["benchmark"]
    calendar_hash = _file_hash(calendar_path) if calendar_path.exists() else "missing"
    try:
        expected_close = completed_day(day, calendar_path, now)
    except (OSError, ValueError, EOFError):
        expected_close = None
    workflow_input_hash = _hash_json({
        "schema_version": COLLECTOR_SCHEMA_VERSION,
        "date": day,
        "watchlist": wl,
        "trading_calendar_hash": calendar_hash,
        "expected_close": expected_close,
    })
    ledger = ledger or RunLedger(root / ".runs" / "ledger.db")
    run_id = ledger.start_run(
        "data_collect",
        day,
        trigger_source="collector",
        input_hash=workflow_input_hash,
        metadata={"schema_version": COLLECTOR_SCHEMA_VERSION},
    )
    manifest: dict = {
        "date": day,
        "collector_run_id": run_id,
        "collector_input_hash": workflow_input_hash,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sources": {},
        "ok": True,
    }

    def record(name: str, ok: bool, detail: str = "") -> None:
        manifest["sources"][name] = {"ok": ok, "detail": detail}
        if not ok:
            manifest["ok"] = False
        print(f"[{'OK' if ok else 'FAIL'}] {name} {detail}")

    def save(path: Path, value: object) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def source(
        name: str,
        artifact_name: str,
        callback,
        *,
        input_hash: str = workflow_input_hash,
        reuse: bool = True,
    ):
        artifact = out_dir / artifact_name
        previous = ledger.latest_step(run_id, name)
        if (
            reuse and previous
            and previous["status"] == "succeeded"
            and previous.get("input_hash") == input_hash
            and artifact.exists()
            and previous.get("output_hash") == _file_hash(artifact)
        ):
            detail = previous["metadata"].get("detail", "reused")
            record(name, True, f"reused · {detail}")
            return json.loads(artifact.read_text(encoding="utf-8"))
        step_id = ledger.start_step(run_id, name, input_hash=input_hash)
        try:
            value, detail = callback()
            save(artifact, value)
            ledger.finish_step(
                step_id,
                output_hash=_file_hash(artifact),
                artifact_path=str(artifact.relative_to(root.parent)),
                metadata={"detail": detail},
            )
            record(name, True, detail)
            return value
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:300]
            ledger.finish_step(step_id, status="failed", error=detail)
            record(name, False, detail)
            return None

    calendar_step = ledger.start_step(
        run_id,
        "trading_calendar",
        input_hash=calendar_hash,
    )
    try:
        trading_days = load_trading_days(calendar_path)
        if not trading_days:
            raise ValueError("empty trading calendar")
        ordered_days = sorted(trading_days)
        calendar_detail = (
            f"{len(trading_days)} 日 · {ordered_days[0]} 至 {ordered_days[-1]}"
        )
        ledger.finish_step(
            calendar_step,
            output_hash=calendar_hash,
            artifact_path=str(calendar_path),
            metadata={"detail": calendar_detail},
        )
        record("trading_calendar", True, calendar_detail)
    except Exception as exc:
        trading_days = set()
        calendar_detail = f"{type(exc).__name__}: {exc}"[:300]
        ledger.finish_step(
            calendar_step,
            status="failed",
            error=calendar_detail,
        )
        record("trading_calendar", False, calendar_detail)

    secids = [stock["secid"] for stock in stocks] + [bench["secid"]]
    expected_codes = {stock["code"] for stock in stocks} | {bench["code"]}

    def archive_callback():
        receipt = capture_closing(day, team=root, calendar_path=calendar_path, now=now, ledger=ledger)
        return receipt, f"{receipt['date']} / {receipt['payload_hash']}"

    closing = source("execution_archive", "execution_archive.json", archive_callback, reuse=False)
    manifest["execution_archive"] = closing

    def require_snapshot_coverage(value: dict, source_name: str) -> dict:
        missing = sorted(expected_codes - set(value))
        if missing:
            raise RuntimeError(f"{source_name} snapshot missing codes: {missing}")
        return value

    def snapshot_callback():
        try:
            value = require_snapshot_coverage(fetch_snapshot(secids), "eastmoney")
            return value, f"东财 {len(value)} 只"
        except Exception as primary:
            value = require_snapshot_coverage(fetch_snapshot_tx(secids), "tencent")
            return value, f"腾讯兜底 {len(value)} 只 (东财失败: {primary!r})"[:240]

    snap = source("snapshot", "snapshot.json", snapshot_callback) or {}
    technical_dates: dict[str, str] = {}
    for stock in stocks + [bench]:
        code, secid = stock["code"], stock["secid"]

        def technical_callback(secid=secid):
            rows = fetch_kline(secid)
            indicators = compute_indicators(rows)
            indicators["recent_klines"] = rows[-30:]
            return indicators, f"as_of {indicators['as_of']}"

        technical = source(
            f"technical_{code}", f"technical_{code}.json", technical_callback
        )
        if technical and technical.get("as_of"):
            technical_dates[code] = str(technical["as_of"])
        if stock is bench:
            continue

        def fundamental_callback(code=code, secid=secid):
            financials = fetch_financials(code, secid)
            announcements = fetch_announcements(code)
            value = {"financials": financials, "announcements": announcements}
            detail = f"{len(financials['reports'])} 期财报, {len(announcements)} 条公告"
            return value, detail

        source(
            f"fundamental_{code}",
            f"fundamental_{code}.json",
            fundamental_callback,
        )

    def news_callback():
        items = fetch_fastnews()
        return {"fast_news": items}, f"{len(items)} 条快讯"

    source("news", "news.json", news_callback)

    session_input_hash = _hash_json({
        "technical_dates": technical_dates,
        "expected_codes": sorted(expected_codes),
        "trading_calendar_hash": calendar_hash,
        "execution_archive": closing,
    })
    previous_session = ledger.latest_step(run_id, "session_alignment")
    if (
        previous_session
        and previous_session["status"] == "succeeded"
        and previous_session.get("input_hash") == session_input_hash
        and previous_session["metadata"].get("result")
    ):
        session = previous_session["metadata"]["result"]
        record("session_alignment", True, f"reused · {session['detail']}")
    else:
        session_step = ledger.start_step(
            run_id, "session_alignment", input_hash=session_input_hash
        )
        session = validate_completed_session(
            day,
            technical_dates,
            expected_codes=expected_codes,
            trading_days=trading_days,
            now=now,
        )
        if session["ok"] and (not closing or session["latest_trade_date"] != closing["date"]):
            session = {**session, "ok": False,
                       "detail": "技术数据日期与最近已完成的原始收盘行情归档不一致或行情归档缺失"}
        ledger.finish_step(
            session_step,
            status="succeeded" if session["ok"] else "failed",
            output_hash=_hash_json(session),
            error=None if session["ok"] else session["detail"],
            metadata={"result": session},
        )
        record("session_alignment", session["ok"], session["detail"])
    latest_trade_date = session.get("latest_trade_date") if session["ok"] else None

    if latest_trade_date:
        sentiment_input_hash = _hash_json({
            "trade_date": latest_trade_date,
            "snapshot_hash": _hash_json(snap),
        })

        breadth = source(
            "market_breadth", "market_breadth.json",
            lambda: (fetch_market_breadth(latest_trade_date), f"SH/SZ snapshot {latest_trade_date}"),
        )
        if breadth is not None:
            breadth = {key: value for key, value in breadth.items() if key != "pages"}
            breadth["source_file"] = "market_breadth.json"
            breadth["source_hash"] = _file_hash(out_dir / "market_breadth.json")
        sentiment_input_hash = _hash_json({"inputs": sentiment_input_hash, "breadth": breadth})

        def sentiment_callback():
            if breadth is None:
                raise ValueError("market breadth source unavailable")
            zt = fetch_zt_pool(latest_trade_date)
            dt = fetch_dt_pool(latest_trade_date)
            value = {
                "trade_date": latest_trade_date,
                "zt_pool": zt,
                "dt_pool": dt,
                "breadth": breadth,
                "watchlist_sentiment": {
                    code: {
                        "turnover_rate": quote.get("turnover_rate"),
                        "volume_ratio": quote.get("volume_ratio"),
                        "pct_chg": quote.get("pct_chg"),
                    }
                    for code, quote in snap.items()
                },
            }
            return value, f"涨停 {zt['zt_count']} / 跌停 {dt['dt_count']}"

        source(
            "sentiment",
            "sentiment.json",
            sentiment_callback,
            input_hash=sentiment_input_hash,
        )
    else:
        detail = "session_alignment 未通过，停止情绪日期派生"
        sentiment_step = ledger.start_step(
            run_id,
            "sentiment",
            input_hash=_hash_json({"session": session}),
        )
        ledger.finish_step(
            sentiment_step,
            status="skipped",
            error=detail,
            metadata={"reason": detail},
        )
        record("sentiment", False, detail)

    manifest["latest_trade_date"] = latest_trade_date
    manifest["session"] = session
    manifest["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    manifest_path = out_dir / "manifest.json"
    manifest_step = ledger.start_step(run_id, "manifest", input_hash=workflow_input_hash)
    save(manifest_path, manifest)
    ledger.finish_step(
        manifest_step,
        status="succeeded" if manifest["ok"] else "failed",
        output_hash=_file_hash(manifest_path),
        artifact_path=str(manifest_path.relative_to(root.parent)),
        error=None if manifest["ok"] else "one or more data gates failed",
    )
    ledger.finish_run(
        run_id,
        status="succeeded" if manifest["ok"] else "failed",
        data_asof=latest_trade_date,
        error=None if manifest["ok"] else "one or more data gates failed",
        metadata={"manifest": str(manifest_path.relative_to(root.parent))},
    )
    print(f"\ncontext pack -> {out_dir}")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()
    manifest = collect_day(args.date)
    if not manifest["ok"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
