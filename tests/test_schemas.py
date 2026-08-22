# -*- coding: utf-8 -*-
"""schemas.py：仓位/入场价解析与方向判定"""
import pytest

from trading_team.schemas import (
    SchemaError, compute_executable, is_buy, is_sell, parse_entry, parse_pct,
    validate_plan,
)


class TestParsePct:
    def test_float(self):
        assert parse_pct(0.15) == 0.15

    def test_int_percent(self):
        assert parse_pct(15) == 0.15

    def test_numeric_string(self):
        assert parse_pct("0.2") == 0.2

    def test_percent_string(self):
        assert parse_pct("15%") == 0.15

    def test_none_is_zero(self):
        assert parse_pct(None) == 0.0

    def test_chinese_text_rejected(self):
        with pytest.raises(SchemaError):
            parse_pct("15%（8%+7% 分批）")

    def test_bool_rejected(self):
        with pytest.raises(SchemaError):
            parse_pct(True)

    def test_out_of_range(self):
        with pytest.raises(SchemaError):
            parse_pct(150)  # 150% 仓位不可能


class TestParseEntry:
    def test_none(self):
        assert parse_entry(None) is None

    def test_dash(self):
        assert parse_entry("—") is None

    def test_list(self):
        assert parse_entry([53.5, "52.2"]) == [53.5, 52.2]

    def test_slash_numbers(self):
        assert parse_entry("53.5 / 52.2") == [53.5, 52.2]

    def test_text_rejected(self):
        with pytest.raises(SchemaError):
            parse_entry("放量突破 408.6 回踩不破")


class TestDirection:
    @pytest.mark.parametrize("d,pct,expect", [
        ("买入", 0.15, True), ("条件买入", 0.15, True), ("加仓", 0.1, True),
        ("观望", 0.0, False), ("回避", 0.0, False), ("卖出", 0.0, False),
        ("持有", 0.1, False), ("观望（回避）", 0.0, False),
    ])
    def test_is_buy(self, d, pct, expect):
        assert is_buy(d, pct) == expect

    @pytest.mark.parametrize("d,expect", [
        ("卖出", True), ("减持", True), ("回避", True), ("观望（回避）", True),
        ("买入", False), ("观望", False), ("持有", False),
    ])
    def test_is_sell(self, d, expect):
        assert is_sell(d) == expect


class TestValidatePlan:
    def test_good_plan(self):
        assert validate_plan({"code": "601318", "direction": "买入",
                              "position_pct": 0.15, "entry": [53.5]}) == []

    def test_bad_code(self):
        assert validate_plan({"code": "abc", "direction": "买入",
                              "position_pct": 0.1})

    def test_direction_pct_conflict(self):
        problems = validate_plan({"code": "601318", "direction": "观望",
                                  "position_pct": 0.15})
        assert any("矛盾" in p for p in problems)

    def test_strict_string_pct(self):
        problems = validate_plan({"code": "601318", "direction": "买入",
                                  "position_pct": "15%"}, strict=True)
        assert any("数字类型" in p for p in problems)


class TestComputeExecutable:
    def plan(self, **kw):
        base = {"code": "601318", "direction": "买入", "position_pct": 0.15,
                "approval": "approved"}
        base.update(kw)
        return base

    def test_approved_buy_market_ok(self):
        ok, _ = compute_executable(self.plan(), market_ok=True)
        assert ok

    def test_market_not_ok_blocks_buy(self):
        ok, reason = compute_executable(self.plan(), market_ok=False)
        assert not ok and "market_ok" in reason

    def test_pending_not_executable(self):
        ok, _ = compute_executable(self.plan(approval="pending"), market_ok=True)
        assert not ok

    def test_rejected_not_executable(self):
        ok, _ = compute_executable(self.plan(approval="rejected"), market_ok=True)
        assert not ok

    def test_reduced_is_executable(self):
        ok, _ = compute_executable(self.plan(approval="reduced"), market_ok=True)
        assert ok

    def test_zero_pct_watch_market_not_ok(self):
        # 观望 0% 仓位：market_ok=false 也可"执行"（实际无动作）
        ok, _ = compute_executable(
            self.plan(direction="观望", position_pct=0), market_ok=False)
        assert ok

    def test_unparsable_pct_blocked(self):
        ok, reason = compute_executable(
            self.plan(position_pct="15%（分批）"), market_ok=True)
        assert not ok and "position_pct" in reason
