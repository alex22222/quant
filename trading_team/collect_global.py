# -*- coding: utf-8 -*-
"""全球宏观雷达 — 晨会前采集隔夜外盘/恐慌指数/亚太股指/货币政策/A股热点舆情。

用法:  .venv/bin/python -m trading_team.collect_global [--date YYYY-MM-DD]

产出:  trading_team/context/<date>/global_macro.json
  ├── overnight_us       隔夜美股三大指数（腾讯行情）
  ├── vix                恐慌指数（CBOE 官方历史序列，含 5 日变化）
  ├── asia               日经225 / 恒生 / KOSPI（新浪 + 东财补）
  ├── commodities        纽约黄金 / 纽约原油（新浪国际期货）
  ├── forex              离岸人民币 USDCNH（东财，允许缺失）
  ├── monetary_policy    美联储货币政策新闻稿（Fed 官方 RSS）
  ├── a_share_hotspots   行业板块涨跌榜（东财，失败时回退涨停池题材聚类）
  └── global_news        新浪 7×24 全球财经快讯

纪律: 单源失败不中断；每个 section 带 ok/source/asof 元信息，写入 manifest 段。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
H_EM = {**H, "Referer": "https://quote.eastmoney.com/"}
H_SINA = {**H, "Referer": "https://finance.sina.com.cn"}
TIMEOUT = 12


def _req(url: str, params: dict | None = None, headers: dict = H, retries: int = 3) -> str:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


def _req_gbk(url: str, params: dict | None = None, headers: dict = H_SINA, retries: int = 3) -> str:
    """新浪等 GBK 编码源：直接按原始字节解码，避免双重转码损坏中文。"""
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            return r.content.decode("gbk", "ignore")
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last  # type: ignore[misc]


def _tx_quote(symbols: list[str]) -> dict:
    """腾讯行情（美股/港股指数），v_xxx 波浪线格式。"""
    text = _req("https://qt.gtimg.cn/q=" + ",".join(symbols))
    text = text.encode("latin1", "ignore").decode("gbk", "ignore") if "v_" in text else text
    out = {}
    for m in re.finditer(r'v_(\w+)="([^"]*)"', text):
        f = m.group(2).split("~")
        if len(f) < 35 or not f[3]:
            continue
        def num(i):
            try:
                return float(f[i])
            except (ValueError, IndexError):
                return None
        out[m.group(1)] = {"name": f[1], "close": num(3), "change": num(31),
                           "pct_chg": num(32), "time": f[30] if len(f) > 30 else None}
    return out


# ---------- 1. 隔夜美股 ----------
def fetch_overnight_us() -> dict:
    q = _tx_quote(["usDJI", "usIXIC", "usINX"])
    label = {"usDJI": "道琼斯", "usIXIC": "纳斯达克", "usINX": "标普500"}
    data = {label[k]: v for k, v in q.items() if k in label}
    if not data:
        raise RuntimeError("美股指数为空")
    asof = max((v.get("time") or "" for v in data.values()))
    return {"ok": True, "source": "腾讯行情", "asof": asof, "data": data}


# ---------- 2. VIX（CBOE 官方） ----------
def fetch_vix() -> dict:
    text = _req("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv")
    rows = list(csv.DictReader(io.StringIO(text)))[-6:]
    closes = [round(float(r["CLOSE"]), 2) for r in rows]
    chg5d = round(closes[-1] - closes[0], 2)
    level = ("极度恐慌" if closes[-1] >= 35 else "恐慌" if closes[-1] >= 25
             else "偏高" if closes[-1] >= 20 else "平静" if closes[-1] >= 13 else "极度平静")
    return {"ok": True, "source": "CBOE 官方", "asof": rows[-1]["DATE"],
            "data": {"close": closes[-1], "level": level, "chg_5d": chg5d,
                     "recent": [{"date": r["DATE"], "close": round(float(r["CLOSE"]), 2)} for r in rows]}}


# ---------- 3. 亚太股指 ----------
def fetch_asia() -> dict:
    data, asof = {}, []
    try:
        text = _req_gbk("https://hq.sinajs.cn/list=int_nikkei,int_hangseng")
        for m in re.finditer(r'hq_str_(\w+)="([^"]*)"', text):
            f = m.group(2).split(",")
            if len(f) >= 4 and f[1]:
                data[f[0]] = {"close": float(f[1]), "change": float(f[2]), "pct_chg": float(f[3])}
    except Exception:
        pass
    # KOSPI：东财（允许失败）
    try:
        raw = _req("https://push2.eastmoney.com/api/qt/ulist.np/get",
                   {"secids": "100.KS11", "fields": "f2,f3,f14", "fltt": 2, "invt": 2}, H_EM)
        diff = (json.loads(raw).get("data") or {}).get("diff") or []
        if diff:
            data["韩国KOSPI"] = {"close": diff[0].get("f2"), "pct_chg": diff[0].get("f3")}
    except Exception:
        data.setdefault("韩国KOSPI", None)
    if not data:
        raise RuntimeError("亚太指数全部不可用")
    return {"ok": True, "source": "新浪/东财", "asof": date.today().isoformat(),
            "data": data, "note": "KOSPI 数据源不稳定，允许缺失"}


# ---------- 4. 大宗商品 ----------
def fetch_commodities() -> dict:
    text = _req_gbk("https://hq.sinajs.cn/list=hf_GC,hf_CL")
    out = {}
    for m in re.finditer(r'hq_str_(\w+)="([^"]*)"', text):
        f = m.group(2).split(",")
        if len(f) >= 14 and f[0]:
            out[f[13]] = {"close": float(f[0]), "prev_settle": float(f[7]) if f[7] else None,
                          "date": f[12]}
    if not out:
        raise RuntimeError("商品行情为空")
    return {"ok": True, "source": "新浪国际期货", "asof": date.today().isoformat(), "data": out}


# ---------- 5. 离岸人民币 ----------
def fetch_forex() -> dict:
    raw = _req("https://push2.eastmoney.com/api/qt/ulist.np/get",
               {"secids": "133.USDCNH", "fields": "f2,f3,f14", "fltt": 2, "invt": 2}, H_EM)
    diff = (json.loads(raw).get("data") or {}).get("diff") or []
    if not diff:
        raise RuntimeError("外汇行情为空")
    return {"ok": True, "source": "东财", "asof": date.today().isoformat(),
            "data": {"USDCNH": {"close": diff[0].get("f2"), "pct_chg": diff[0].get("f3")}}}


# ---------- 6. 美联储货币政策（官方 RSS） ----------
def fetch_monetary() -> dict:
    last = None
    for i in range(3):
        try:
            r = requests.get("https://www.federalreserve.gov/feeds/press_monetary.xml",
                             headers=H, timeout=TIMEOUT)
            r.raise_for_status()
            text = r.content.decode("utf-8-sig", "ignore")  # 官方源带 BOM
            break
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    else:
        raise last  # type: ignore[misc]
    root = ET.fromstring(text)
    items = []
    for it in root.iter("item"):
        items.append({"title": (it.findtext("title") or "").strip(),
                      "date": (it.findtext("pubDate") or "").strip(),
                      "link": (it.findtext("link") or "").strip()})
        if len(items) >= 6:
            break
    if not items:
        raise RuntimeError("Fed RSS 无条目")
    return {"ok": True, "source": "Federal Reserve 官方 RSS", "asof": items[0]["date"],
            "data": items}


# ---------- 7. A股热点 ----------
def fetch_hotspots(day_dir: Path) -> dict:
    # 主源：东财行业板块榜（涨/跌各 10）
    try:
        def board(po):
            raw = _req("https://push2.eastmoney.com/api/qt/clist/get",
                       {"pn": 1, "pz": 10, "po": po, "np": 1, "fltt": 2, "invt": 2,
                        "fid": "f3", "fs": "m:90+t:2", "fields": "f12,f14,f3"}, H_EM)
            return [{"name": d.get("f14"), "pct_chg": d.get("f3")}
                    for d in ((json.loads(raw).get("data") or {}).get("diff") or [])]
        up, down = board(1), board(0)
        if up:
            return {"ok": True, "source": "东财行业板块榜", "asof": date.today().isoformat(),
                    "data": {"top_up": up, "top_down": down}}
    except Exception:
        pass
    # 回退：涨停池题材聚类（复用当日 sentiment.json）
    senti = day_dir / "sentiment.json"
    if senti.exists():
        s = json.loads(senti.read_text(encoding="utf-8"))
        samples = (s.get("zt_pool") or {}).get("samples") or []
        cluster: dict[str, int] = {}
        for x in samples:
            r = x.get("reason") or "其他"
            cluster[r] = cluster.get(r, 0) + 1
        top = sorted(cluster.items(), key=lambda kv: -kv[1])[:8]
        return {"ok": True, "source": "涨停池题材聚类（回退）",
                "asof": s.get("trade_date"),
                "data": {"zt_theme_cluster": [{"theme": k, "count": v} for k, v in top]}}
    raise RuntimeError("热点数据不可用")


# ---------- 8. 全球快讯（新浪 7×24） ----------
def fetch_global_news() -> dict:
    raw = _req("https://zhibo.sina.com.cn/api/zhibo/feed",
               {"page": 1, "page_size": 20, "zhibo_id": 152, "tag_id": 0, "dire": "f",
                "dpc": 1, "pagesize": 20}, H_SINA)
    feed = (((json.loads(raw).get("result") or {}).get("data") or {}).get("feed") or {}).get("list") or []
    items = []
    for n in feed[:15]:
        text = re.sub(r"<[^>]+>", "", n.get("rich_text") or "").strip()
        if text:
            items.append({"time": n.get("create_time"), "text": text[:220]})
    if not items:
        raise RuntimeError("新浪 7x24 无快讯")
    return {"ok": True, "source": "新浪 7×24", "asof": items[0].get("time"), "data": items}


# ---------- 主流程 ----------
SECTIONS = [
    ("overnight_us", fetch_overnight_us),
    ("vix", fetch_vix),
    ("asia", fetch_asia),
    ("commodities", fetch_commodities),
    ("forex", fetch_forex),
    ("monetary_policy", fetch_monetary),
    ("a_share_hotspots", None),  # 特殊：需要 day_dir
    ("global_news", fetch_global_news),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()
    day_dir = ROOT / "context" / args.date
    day_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {"date": args.date, "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "ok": True, "sections": {}}
    for name, fn in SECTIONS:
        try:
            sec = fetch_hotspots(day_dir) if fn is None else fn()
            result["sections"][name] = sec
            print(f"[{'OK' if sec.get('ok') else 'FAIL'}] {name} <- {sec.get('source')} asof={sec.get('asof')}")
        except Exception as e:
            result["sections"][name] = {"ok": False, "error": repr(e)[:200]}
            result["ok"] = False
            print(f"[FAIL] {name} {repr(e)[:120]}")

    out = day_dir / "global_macro.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nglobal macro -> {out}")


if __name__ == "__main__":
    main()
