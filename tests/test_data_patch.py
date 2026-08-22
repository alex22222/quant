# -*- coding: utf-8 -*-
"""patch_bundle_from_context.py：dry-run、重叠日拒写、单位口径拒写、manifest"""
import json

import h5py
import numpy as np
import pytest

from trading_team.patch_bundle_from_context import dt_int, patch_file

STOCK_DTYPE = [('datetime', '<i8'), ('open', '<f8'), ('close', '<f8'),
               ('high', '<f8'), ('low', '<f8'), ('prev_close', '<f8'),
               ('limit_up', '<f8'), ('limit_down', '<f8'),
               ('volume', '<f8'), ('total_turnover', '<f8')]


@pytest.fixture
def h5file(tmp_path):
    """含 601318.XSHG 到 2026-07-31 的迷你 bundle"""
    path = tmp_path / "stocks.h5"
    rows = [(dt_int("2026-07-30"), 52.0, 52.5, 53.0, 51.5, 51.8, 56.98, 46.62, 1e6, 5.25e9),
            (dt_int("2026-07-31"), 52.5, 53.0, 53.5, 52.0, 52.5, 57.75, 47.25, 1e6, 5.3e9)]
    with h5py.File(path, "w") as h:
        h.create_dataset("601318.XSHG", data=np.array(rows, dtype=STOCK_DTYPE))
    return path


def _klines(close_last, dates=("2026-07-31", "2026-08-03"), vol=1e6):
    out = []
    for i, d in enumerate(dates):
        c = close_last if i == 0 else close_last * 1.01
        out.append({"date": d, "open": c, "close": c, "high": c * 1.01,
                    "low": c * 0.99, "volume": vol, "amount": c * vol * 100,
                    "pct_chg": 0.57 if i == 0 else 1.0})
    return out


class TestPatch:
    def test_dry_run_writes_nothing(self, h5file):
        report = {}
        n = patch_file(h5file, "601318.XSHG", _klines(53.0), True, False, report)
        assert n == 1 and report["601318.XSHG"]["rows_added"] == 1
        with h5py.File(h5file, "r") as h:
            assert len(h["601318.XSHG"]) == 2  # 未写入

    def test_apply_appends(self, h5file):
        report = {}
        n = patch_file(h5file, "601318.XSHG", _klines(53.0), True, True, report)
        assert n == 1
        with h5py.File(h5file, "r") as h:
            d = h["601318.XSHG"][:]
            assert len(d) == 3
            assert int(d["datetime"][-1]) == dt_int("2026-08-03")
            assert d["limit_up"][-1] > d["close"][-1] > d["limit_down"][-1]

    def test_overlap_mismatch_rejects(self, h5file):
        report = {}
        n = patch_file(h5file, "601318.XSHG", _klines(55.0), True, True, report)
        assert n == 0 and "重叠日" in report["601318.XSHG"]["rejected"]
        with h5py.File(h5file, "r") as h:
            assert len(h["601318.XSHG"]) == 2  # 拒写

    def test_unit_mismatch_rejects(self, h5file):
        report = {}
        kl = _klines(53.0)
        kl[1]["amount"] = kl[1]["close"] * kl[1]["volume"]  # 少了 ×100 → 单位错误
        n = patch_file(h5file, "601318.XSHG", kl, True, True, report)
        assert n == 0 and "量价单位" in report["601318.XSHG"]["rejected"]

    def test_already_latest_noop(self, h5file):
        report = {}
        n = patch_file(h5file, "601318.XSHG", _klines(53.0, dates=("2026-07-31",)),
                       True, True, report)
        assert n == 0 and report["601318.XSHG"]["rejected"] is None
