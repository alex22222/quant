# -*- coding: utf-8 -*-
"""pipeline/universe.py：历史时点股票池构建测试（策略库审查 P2）"""
import json

import pytest

from pipeline.universe import UniverseBuilder, load_snapshot, save_snapshot


@pytest.fixture(scope="module")
def builder():
    b = UniverseBuilder()
    yield b
    b.close()


class TestPredicates:
    def test_st_excluded(self, builder):
        """ST 当日剔除（000004.XSHE 2026-07-10 为 ST 日，来自 bundle st_stock_days）"""
        assert builder.is_st("000004.XSHE", 20260710)
        assert not builder.is_st("600519.XSHG", 20240102)

    def test_suspended_excluded(self, builder):
        """停牌当日剔除（000001.XSHE 2005-01-11 停牌，来自 bundle suspended_days）"""
        assert builder.is_suspended("000001.XSHE", 20050111)
        assert not builder.is_suspended("600519.XSHG", 20240102)

    def test_min_listing_age(self, builder):
        """上市不足 365 天剔除"""
        assert builder.listed_enough("600519.XSHG", "2024-01-02")   # 2001 上市
        assert not builder.listed_enough("600519.XSHG", "2001-09-01")  # 上市当月

    def test_unknown_code_fails_closed(self, builder):
        """未知代码一律 fail closed"""
        assert not builder.listed_enough("FAKE.XSHG", "2024-01-02")
        assert builder.turnover_20d("FAKE.XSHG", 20240102) is None


class TestBuild:
    def test_deterministic(self, builder):
        """同一日两次构建结果完全一致"""
        a = builder.build("2024-01-02", n=50)
        b = builder.build("2024-01-02", n=50)
        assert a == b and len(a) == 50

    def test_members_meet_predicates(self, builder):
        """池内成员必须通过全部过滤条件"""
        codes = builder.build("2024-01-02", n=30)
        for c in codes:
            assert builder.listed_enough(c, "2024-01-02")
            assert not builder.is_st(c, 20240102)
            assert not builder.is_suspended(c, 20240102)
            assert builder.turnover_20d(c, 20240102) >= 5e7

    def test_sorted_by_liquidity(self, builder):
        """按 20 日日均成交额降序"""
        codes = builder.build("2024-01-02", n=20)
        tos = [builder.turnover_20d(c, 20240102) for c in codes]
        assert tos == sorted(tos, reverse=True)

    def test_no_data_day_returns_empty(self, builder):
        """非交易日（数据不存在）返回空池而非报错"""
        assert builder.build("2024-01-01", n=10) == []  # 元旦


class TestSnapshot:
    def test_roundtrip(self, builder, tmp_path, monkeypatch):
        """快照写入后可读回，内容一致"""
        import pipeline.universe as u
        monkeypatch.setattr(u, "SNAP_DIR", tmp_path)
        path = save_snapshot(builder, "2024-01-02", n=10)
        assert path.exists()
        codes = load_snapshot("2024-01-02")
        assert codes == builder.build("2024-01-02", n=10)
        assert json.loads(path.read_text())["count"] == 10

    def test_missing_snapshot_fails_closed(self, tmp_path, monkeypatch):
        import pipeline.universe as u
        monkeypatch.setattr(u, "SNAP_DIR", tmp_path)
        assert load_snapshot("1999-01-01") is None
