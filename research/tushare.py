"""Tushare Pro HTTP adapter. Explicit endpoint/parameters, paginated, no auto-purchase.

Financial restatements and historical industry exits are only visible from local
observation time unless an independently verified historical vintage is supplied.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pandas as pd
import requests

from .data import ParquetStore

# endpoint -> canonical table, economic date column, public date column
ENDPOINTS = {
    "daily": ("daily", "trade_date", "trade_date"),
    "adj_factor": ("adj_factor", "trade_date", "trade_date"),
    "daily_basic": ("daily_basic", "trade_date", "trade_date"),
    "fina_indicator": ("fundamentals", "end_date", "ann_date"),
    "index_member_all": ("industry", "in_date", "in_date"),
    "index_weight": ("membership", "trade_date", "trade_date"),
    "stock_basic": ("securities", "list_date", "list_date"),
    "stk_limit": ("limits", "trade_date", "trade_date"),
    "suspend_d": ("suspensions", "trade_date", "trade_date"),
    "stock_st": ("st", "trade_date", "trade_date"),
    "trade_cal": ("calendar", "cal_date", "cal_date"),
    "index_daily": ("index_daily", "trade_date", "trade_date"),
}

FIELDS = {
    "fina_indicator": "ts_code,ann_date,end_date,roe,ocf_to_profit,update_flag",
    "stock_basic": "ts_code,symbol,name,market,list_status,list_date,delist_date",
}


def normalize(endpoint: str, frame: pd.DataFrame, observed_at: str) -> pd.DataFrame:
    _, economic, public = ENDPOINTS[endpoint]
    out = frame.copy()
    out["data_date"] = pd.to_datetime(out[economic], format="%Y%m%d", errors="raise").dt.strftime("%Y-%m-%d")
    visible = pd.to_datetime(out[public], format="%Y%m%d", errors="raise")
    # Announcement dates without a time are usable only on the following day.
    if endpoint == "fina_indicator":
        from datetime import timedelta
        visible = pd.to_datetime(out[public].map(
            lambda text: (datetime.strptime(text, "%Y%m%d") + timedelta(days=1)).strftime("%Y-%m-%d")))
    # Never backdate revised financials, listing status or membership exit metadata.
    if endpoint in {"fina_indicator", "index_member_all", "stock_basic", "index_weight"}:
        today = pd.Timestamp(observed_at).tz_convert("Asia/Shanghai").tz_localize(None).normalize()
        visible = visible.clip(lower=today)
    out["available_date"] = visible.dt.strftime("%Y-%m-%d")
    out["observed_at"] = observed_at
    out["source"] = f"tushare:{endpoint}"
    out["vintage"] = "retrospective_vendor_snapshot"
    out["source_revision"] = out["ann_date"].astype(str) if endpoint == "fina_indicator" else ""
    return out


class TushareSource:
    def __init__(self, token: str | None = None, session=None):
        self.token = token or os.environ.get("TUSHARE_TOKEN")
        if not self.token:
            raise RuntimeError("TUSHARE_TOKEN missing; no data downloaded")
        self.session = session or requests.Session()

    def fetch(self, endpoint: str, params: dict, page_size: int = 100) -> pd.DataFrame:
        if endpoint not in ENDPOINTS or not 1 <= page_size <= 100:
            raise ValueError("unsupported endpoint or unsafe page size")
        pages, previous = [], None
        # 100 is also safe for the non-VIP fina_indicator endpoint.
        for offset in range(0, 1000000, page_size):
            response = self.session.post("https://api.tushare.pro", json={
                "api_name": endpoint, "token": self.token,
                "params": {**params, "limit": page_size, "offset": offset},
                "fields": FIELDS.get(endpoint, "")}, timeout=30)
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 0:
                # Vendor messages may contain request details. Do not echo secrets.
                raise RuntimeError(f"Tushare {endpoint} rejected request (code={body.get('code')})")
            data = body.get("data") or {}
            page = pd.DataFrame(data.get("items") or [], columns=data.get("fields") or [])
            if page.empty:
                break
            fingerprint = page.to_json()
            if fingerprint == previous:
                raise RuntimeError("pagination did not advance; refusing partial coverage")
            previous = fingerprint
            pages.append(page)
            if len(page) < page_size:
                break
        else:
            raise RuntimeError("pagination safety limit reached")
        return pd.concat(pages, ignore_index=True) if pages else pd.DataFrame()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint", choices=ENDPOINTS)
    parser.add_argument("--params", default="{}", help='explicit JSON parameters')
    parser.add_argument("--root", type=Path, default=Path("data/research"))
    args = parser.parse_args()
    source = TushareSource()
    params = json.loads(args.params)
    if args.endpoint == "stock_basic" and params.get("list_status") not in {"L", "D", "P"}:
        parser.error("request L, D and P separately; never silently keep only current listings")
    frame = source.fetch(args.endpoint, params)
    if frame.empty:
        raise RuntimeError("empty response; coverage remains unverified")
    now = datetime.now(timezone.utc).isoformat()
    print(json.dumps(ParquetStore(args.root).append(ENDPOINTS[args.endpoint][0],
                     normalize(args.endpoint, frame, now)), indent=2))


if __name__ == "__main__":
    main()
