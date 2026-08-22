# -*- coding: utf-8 -*-
"""schemas.validate_portfolio：组合级确定性风控"""
from trading_team.schemas import validate_portfolio


def _p(code, direction="买入", pct=0.15, approval="approved"):
    return {"code": code, "direction": direction,
            "position_pct": pct, "approval": approval}


class TestPortfolioRules:
    def test_clean(self):
        plans = [_p("601318", pct=0.15), _p("600036", pct=0.10)]
        assert validate_portfolio(plans, market_ok=True) == []

    def test_single_position_cap(self):
        problems = validate_portfolio([_p("601318", pct=0.30)], market_ok=True)
        assert any("单票仓位" in p for p in problems)

    def test_total_buy_cap(self):
        plans = [_p(c, pct=0.25) for c in
                 ("601318", "600036", "600519", "300750")]
        problems = validate_portfolio(plans, market_ok=True)
        assert any("合计" in p for p in problems)

    def test_max_new_buys(self):
        plans = [_p(f"60000{i}", pct=0.10) for i in range(6)]
        problems = validate_portfolio(plans, market_ok=True)
        assert any("笔数" in p for p in problems)

    def test_pending_plans_not_counted(self):
        plans = [_p("601318", pct=0.30, approval="pending")]
        assert validate_portfolio(plans, market_ok=True) == []

    def test_market_not_ok_with_approved_buy(self):
        problems = validate_portfolio([_p("601318")], market_ok=False)
        assert any("market_ok=false" in p for p in problems)

    def test_watch_plans_ignored(self):
        plans = [_p("601318", direction="观望", pct=0)]
        assert validate_portfolio(plans, market_ok=False) == []

    def test_unparsable_pct_skipped(self):
        plans = [_p("601318", pct="15%（分批）")]
        # 解析失败不在这里报错（由 validate_plan/compute_executable 负责）
        assert validate_portfolio(plans, market_ok=True) == []
