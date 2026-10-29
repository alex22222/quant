"""Join provider tables on conservative visibility; historical gaps fail closed."""
from __future__ import annotations

import pandas as pd

from .data import ParquetStore


def index_membership(rows: pd.DataFrame, day: str, index_code: str) -> pd.DataFrame:
    rows = rows.loc[(rows.index_code == index_code) & (rows.data_date <= day)
                    & (rows.available_date <= day)]
    if rows.empty:
        raise ValueError("no visible historical benchmark constituents")
    # An index rebalance is a complete snapshot: removed stocks must not be carried.
    snapshot = rows.loc[rows.data_date == rows.data_date.max()].copy()
    if not pd.to_numeric(snapshot.weight, errors="coerce").ge(0).all():
        raise ValueError("invalid index weights")
    snapshot["benchmark_weight"] = snapshot.weight / 100
    if abs(snapshot.benchmark_weight.sum() - 1) > .005 or snapshot.con_code.duplicated().any():
        raise ValueError("incomplete/duplicate index weight snapshot")
    return snapshot


def visible_panel(store: ParquetStore, day: str, knowledge_at: str,
                  index_code: str = "000905.SH") -> pd.DataFrame:
    """No fallback to current constituents/industry/financials or fixed blue chips."""
    tables = {name: store.asof(name, day, knowledge_at) for name in (
        "daily", "adj_factor", "daily_basic", "industry", "fundamentals", "membership")}
    members = index_membership(tables["membership"], day, index_code)
    panel = members.rename(columns={"con_code": "ts_code"})[["ts_code", "benchmark_weight"]]
    for name in ("daily", "adj_factor", "daily_basic"):
        frame = tables[name].loc[tables[name].data_date == day]
        columns = {"daily": ["open", "close", "vol", "amount"],
                   "adj_factor": ["adj_factor"],
                   "daily_basic": ["pe_ttm", "pb", "total_mv", "turnover_rate"]}[name]
        if frame.ts_code.duplicated().any():
            raise ValueError(f"ambiguous {name} per security")
        panel = panel.merge(frame[["ts_code", *columns]], on="ts_code", how="left", validate="one_to_one")
    industry = tables["industry"].copy()
    if "out_date" in industry:
        exit_date = pd.to_datetime(industry.out_date.replace("", None), errors="coerce")
        industry = industry.loc[exit_date.isna() | (exit_date > pd.Timestamp(day))]
    industry = industry.sort_values(["data_date", "available_date"]).drop_duplicates("ts_code", keep="last")
    panel = panel.merge(industry[["ts_code", "l1_code"]], on="ts_code", how="left", validate="one_to_one")
    financial = tables["fundamentals"].sort_values(["data_date", "available_date"]).drop_duplicates("ts_code", keep="last")
    financial = financial.rename(columns={"available_date": "financial_available_date"})
    panel = panel.merge(financial[["ts_code", "roe", "ocf_to_profit", "financial_available_date"]],
                        on="ts_code", how="left", validate="one_to_one")
    if panel.isna().any().any():
        raise ValueError("incomplete PIT panel; no silent complete-case survivor filtering")
    panel["date"] = day
    panel["available_date"] = panel.financial_available_date
    return panel
