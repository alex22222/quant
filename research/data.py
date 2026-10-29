"""Append-only Parquet observations and DuckDB point-in-time queries.

data_date is the economic date; available_date is the conservative public date;
observed_at is the local vintage. Backfilled vendor records are NOT original vintages.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pandas as pd

TABLE_KEYS = {
    "daily": ["ts_code", "data_date"],
    "adj_factor": ["ts_code", "data_date"],
    "daily_basic": ["ts_code", "data_date"],
    "fundamentals": ["ts_code", "data_date"],
    "industry": ["ts_code", "data_date", "l3_code"],
    "membership": ["index_code", "con_code", "data_date"],
    "securities": ["ts_code", "data_date"],
    "limits": ["ts_code", "data_date"],
    "suspensions": ["ts_code", "data_date", "suspend_type"],
    "st": ["ts_code", "data_date"],
    "calendar": ["exchange", "data_date"],
    "index_daily": ["ts_code", "data_date"],
}


def validate_observations(table: str, frame: pd.DataFrame) -> pd.DataFrame:
    keys = TABLE_KEYS[table]
    required = keys + ["available_date", "observed_at", "source", "vintage"]
    missing = set(required) - set(frame.columns)
    if missing:
        raise ValueError(f"{table}: missing columns {sorted(missing)}")
    out = frame.copy()
    if "source_revision" not in out:
        out["source_revision"] = ""
    if out[required].isna().any().any():
        raise ValueError(f"{table}: null keys/visibility")
    for col in ("data_date", "available_date"):
        out[col] = pd.to_datetime(out[col], errors="raise").dt.strftime("%Y-%m-%d")
    out["observed_at"] = pd.to_datetime(out.observed_at, utc=True, errors="raise").dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if (out.available_date < out.data_date).any():
        raise ValueError("visibility precedes economic date")
    if out.duplicated(keys + ["available_date", "observed_at", "source_revision"]).any():
        raise ValueError("ambiguous duplicate observation")
    return out


def select_asof(frame: pd.DataFrame, table: str, day: str,
                knowledge_at: str) -> pd.DataFrame:
    """Pandas reference for the same two-time filter used in DuckDB."""
    cutoff = pd.Timestamp(day).strftime("%Y-%m-%d")
    known = pd.to_datetime(knowledge_at, utc=True)
    rows = frame.loc[(frame.data_date <= cutoff) & (frame.available_date <= cutoff)
                     & (pd.to_datetime(frame.observed_at, utc=True) <= known)]
    ordering = ["available_date", "observed_at"] + (["source_revision"] if "source_revision" in rows else [])
    return rows.sort_values(ordering).drop_duplicates(TABLE_KEYS[table], keep="last")


class ParquetStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def append(self, table: str, frame: pd.DataFrame) -> dict:
        rows = validate_observations(table, frame)
        if rows.empty:
            raise ValueError("empty response cannot establish coverage")
        # The content hash makes repeated identical imports idempotent.
        digest = hashlib.sha256(rows.to_json(orient="records", date_format="iso").encode()).hexdigest()
        target = self.root / table / f"{digest}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            tmp = target.with_suffix(f".{uuid4().hex}.tmp")
            try:
                rows.to_parquet(tmp, index=False, engine="pyarrow")
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
        receipt = {"table": table, "rows": len(rows), "content_hash": digest,
                   "file_hash": hashlib.sha256(target.read_bytes()).hexdigest(),
                   "min_date": rows.data_date.min(), "max_date": rows.data_date.max(),
                   "vintage": sorted(rows.vintage.unique().tolist())}
        target.with_suffix(".json").write_text(json.dumps(receipt, indent=2))
        return receipt

    def asof(self, table: str, day: str, knowledge_at: str) -> pd.DataFrame:
        import duckdb
        keys = TABLE_KEYS[table]  # whitelist identifier
        files = sorted((self.root / table).glob("*.parquet"))
        if not files:
            raise FileNotFoundError(f"no {table} observations")
        with duckdb.connect() as conn:
            relation = conn.read_parquet([str(p) for p in files], union_by_name=True)
            relation.create_view("observations")
            return conn.execute(f"""
                SELECT * EXCLUDE (rn) FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY {','.join(keys)}
                        ORDER BY available_date DESC, observed_at DESC, source_revision DESC) rn
                    FROM observations
                    WHERE data_date <= ? AND available_date <= ?
                      AND CAST(observed_at AS TIMESTAMPTZ) <= CAST(? AS TIMESTAMPTZ)
                ) WHERE rn = 1
            """, [day, day, knowledge_at]).df()
