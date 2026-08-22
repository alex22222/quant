# -*- coding: utf-8 -*-
"""阶段1·数据部门：检查本地数据覆盖与新鲜度，必要时更新 bundle"""
import subprocess
import sys
from datetime import date
from pathlib import Path

import h5py
import numpy as np

from .common import ROOT

BUNDLE = Path.home() / ".rqalpha" / "bundle"
VENV_PY = ROOT / ".venv" / "bin" / "python"
RQALPHA = ROOT / ".venv" / "bin" / "rqalpha"


def main(update=False):
    stocks_h5 = BUNDLE / "stocks.h5"
    if not stocks_h5.exists():
        raise FileNotFoundError("数据包不存在，先运行 rqalpha download-bundle")

    f = h5py.File(stocks_h5, "r")
    n_stocks = len(f.keys())
    sample = f["000001.XSHE"]
    first = int(sample[0]["datetime"])
    last = int(sample[-1]["datetime"])
    f.close()

    last_date = date(int(str(last)[:4]), int(str(last)[4:6]), int(str(last)[6:8]))
    age_days = (date.today() - last_date).days

    updated = False
    if update:
        r = subprocess.run([str(RQALPHA), "download-bundle"],
                           capture_output=True, text=True, timeout=600)
        updated = "successfully" in (r.stdout + r.stderr).lower()

    return {
        "bundle_path": str(BUNDLE),
        "stocks": n_stocks,
        "range": f"{str(first)[:8]} ~ {str(last)[:8]}",
        "latest_trading_day": str(last)[:8],
        "freshness_days": age_days,
        "update_triggered": update,
        "updated": updated,
        "note": "月度包通常在月初更新；freshness_days > 35 时建议 --update",
    }
