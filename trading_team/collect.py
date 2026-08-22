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
import json
import time
from datetime import date, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
           "Referer": "https://quote.eastmoney.com/"}
TIMEOUT = 12


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


def load_watchlist() -> dict:
    return json.loads((ROOT / "watchlist.json").read_text(encoding="utf-8"))


# ---------- 行情快照 ----------
def _secid_to_tx(secid: str) -> str:
    """1.600519 -> sh600519, 0.000063 -> sz000063"""
    mkt, code = secid.split(".")
    return ("sh" if mkt == "1" else "sz") + code


def fetch_snapshot_tx(secids: list[str]) -> dict:
    """腾讯行情快照（兜底源）。"""
    r = requests.get("https://qt.gtimg.cn/q=" + ",".join(_secid_to_tx(s) for s in secids),
                     headers={**HEADERS, "Referer": "https://gu.qq.com/"}, timeout=TIMEOUT)
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

    last = rows[-1]
    recent = rows[-60:]
    return {
        "as_of": last["date"],
        "close": last["close"], "pct_chg": last.get("pct_chg"), "turnover": last.get("turnover"),
        "ma5": ma(closes, 5), "ma10": ma(closes, 10), "ma20": ma(closes, 20), "ma60": ma(closes, 60),
        "dif": round(dif[-1], 3), "dea": round(dea[-1], 3), "macd_bar": round(macd_bar[-1], 3),
        "macd_bar_prev": round(macd_bar[-2], 3),
        "rsi14": rsi14,
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


def fetch_market_breadth() -> dict:
    """全市场涨跌分布（东财 1/0/116 secid 批量快照太重，改用涨跌统计接口）。"""
    url = "https://push2.eastmoney.com/api/qt/clist/get"
    counts = {}
    for name, fs in [("up_gt5", "f3>5"), ("up", "f3>0"), ("down", "f3<0"), ("down_gt5", "f3<-5"), ("flat", "f3=0")]:
        try:
            data = _get(url, {"pn": 1, "pz": 1, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                              "fid": "f3", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                              "fields": "f3", "filter": f"({fs})"})
            counts[name] = ((data.get("data") or {}).get("total")) or 0
        except Exception:
            counts[name] = None
    return counts


# ---------- 主流程 ----------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    out_dir = ROOT / "context" / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    wl = load_watchlist()
    stocks = wl["stocks"]
    bench = wl["benchmark"]
    manifest: dict = {"date": args.date, "started_at": datetime.now().isoformat(timespec="seconds"),
                      "sources": {}, "ok": True}

    def record(name: str, ok: bool, detail: str = ""):
        manifest["sources"][name] = {"ok": ok, "detail": detail}
        if not ok:
            manifest["ok"] = False
        print(f"[{'OK' if ok else 'FAIL'}] {name} {detail}")

    def save(name: str, obj):
        (out_dir / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    # 1) 快照（东财主源，腾讯兜底）
    secids = [s["secid"] for s in stocks] + [bench["secid"]]
    snap: dict = {}
    try:
        snap = fetch_snapshot(secids)
        if not snap:
            raise RuntimeError("empty snapshot")
        record("snapshot", True, f"东财 {len(snap)} 只")
    except Exception as e:
        try:
            snap = fetch_snapshot_tx(secids)
            record("snapshot", True, f"腾讯兜底 {len(snap)} 只 (东财失败: {repr(e)[:80]})")
        except Exception as e2:
            record("snapshot", False, repr(e2)[:200])
    save("snapshot.json", snap)

    # 2) 个股技术 + 基本面
    latest_trade_date = None
    for s in stocks + [bench]:
        code, secid = s["code"], s["secid"]
        try:
            rows = fetch_kline(secid)
            ind = compute_indicators(rows)
            ind["recent_klines"] = rows[-30:]
            latest_trade_date = ind["as_of"]
            save(f"technical_{code}.json", ind)
            record(f"technical_{code}", True, f"as_of {ind['as_of']}")
        except Exception as e:
            record(f"technical_{code}", False, repr(e)[:200])
        if s is bench:
            continue
        try:
            fin = fetch_financials(code, secid)
            ann = fetch_announcements(code)
            save(f"fundamental_{code}.json", {"financials": fin, "announcements": ann})
            record(f"fundamental_{code}", True,
                   f"{len(fin['reports'])} 期财报, {len(ann)} 条公告")
        except Exception as e:
            record(f"fundamental_{code}", False, repr(e)[:200])

    # 3) 新闻
    try:
        news = fetch_fastnews()
        save("news.json", {"fast_news": news})
        record("news", True, f"{len(news)} 条快讯")
    except Exception as e:
        record("news", False, repr(e)[:200])

    # 4) 情绪
    try:
        td = latest_trade_date or args.date
        zt = fetch_zt_pool(td)
        dt = fetch_dt_pool(td)
        breadth = fetch_market_breadth()
        emo = {"trade_date": td, "zt_pool": zt, "dt_pool": dt, "breadth": breadth,
               "watchlist_sentiment": {c: {"turnover_rate": v.get("turnover_rate"),
                                           "volume_ratio": v.get("volume_ratio"),
                                           "pct_chg": v.get("pct_chg")}
                                       for c, v in (snap or {}).items()}}
        save("sentiment.json", emo)
        record("sentiment", True, f"涨停 {zt['zt_count']} / 跌停 {dt['dt_count']}")
    except Exception as e:
        record("sentiment", False, repr(e)[:200])

    manifest["latest_trade_date"] = latest_trade_date
    manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
    save("manifest.json", manifest)
    print(f"\ncontext pack -> {out_dir}")


if __name__ == "__main__":
    main()
