# -*- coding: utf-8 -*-
"""Daily strict replay for gate/no-gate portfolios and CSI300."""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from copy import deepcopy
from datetime import date
from pathlib import Path

from paper.execution_model import EXECUTION_MODEL_VERSION, Bar, portfolio_value
from trading_team.entry_contract import ENTRY_CONTRACT_VERSION, entry_contract_errors, simulate_entry
from trading_team.execution_bars import SCHEMA, _bar_from_row, _cache_document, _cached_bars, load_execution_bars
from trading_team.schemas import is_buy, is_sell, parse_pct
from trading_team.decision_pipeline import canonical_plan_hash
from trading_team.paper_plans import select_paper_plans
from trading_team.exit_contract import EXIT_CONTRACT_VERSION, PORTFOLIO_POLICY_VERSION, exit_contract_errors, simulate_exit
from trading_team.risk_contract import risk_review_summary
from trading_team.state_lock import atomic_write_json, atomic_write_text, state_lock
from trading_team.paper_inventory import INVENTORY_POLICY_VERSION, consume_position

TEAM = Path(__file__).resolve().parent


def _order_dict(plan: dict, status: str, reason: str = "", fill=None) -> dict:
    result = {
        "code": str(plan.get("code", "")),
        "name": plan.get("name", ""),
        "direction": plan.get("direction", ""),
        "status": status,
        "reason": reason,
    }
    if fill is not None:
        result.update({
            "qty": fill.qty, "fill_price": fill.price,
            "amount": fill.amount, "fee": fill.fee,
            "price_type": fill.price_type,
        })
    return result


def _portfolio(plan_doc: dict, bars: dict[str, Bar], initial_cash: float,
               enforce_gate: bool, slippage_rate: float, initial_positions: dict) -> dict:
    cash = initial_cash
    buy_cash = initial_cash
    positions = deepcopy(initial_positions)
    initial_equity = portfolio_value(cash, positions, bars, price_field="prev_close")
    orders = []
    market_ok = bool(plan_doc.get("market_ok", False))
    plans = select_paper_plans(plan_doc)
    if any("protection" in plan for plan in plans):
        return {"initial_cash": initial_cash, "initial_equity": initial_equity,
                "positions": None, "cash": None, "equity": None, "return": None,
                "status": "requires_continuous_state",
                "orders": [_order_dict(plan, "blocked", "跨日保护需要连续账户状态；单日诊断不推断授权")
                           for plan in plans]}
    for plan in plans:
        if not is_sell(str(plan.get("direction", ""))):
            continue
        errors = exit_contract_errors(plan)
        if errors:
            orders.append(_order_dict(plan, "blocked", "exit contract: " + "; ".join(errors)))
            continue
        code = str(plan["code"])
        bar = bars.get(code)
        if bar is None:
            orders.append(_order_dict(plan, "rejected", "无行情"))
            continue
        current = positions.get(code, {"qty": 0})
        fill, _ = simulate_exit(plan, current, bar, plan_doc.get("date"), slippage_rate=slippage_rate)
        orders.append(_order_dict(plan, fill.status, fill.reject_reason, fill))
        if fill.filled:
            cash += fill.amount - fill.fee
            consume_position(current, fill.qty, plan_doc.get("date"))
            if not current["qty"]:
                positions.pop(code)
    for plan in plans:
        pct = parse_pct(plan.get("position_pct"))
        if not is_buy(str(plan.get("direction", "")), pct) or pct <= 0:
            continue
        errors = entry_contract_errors(plan)
        if plan.get("exit") is not None:
            errors.extend(exit_contract_errors(plan))
        if errors:
            orders.append(_order_dict(plan, "blocked", "entry contract: " + "; ".join(errors)))
            continue
        if enforce_gate and not market_ok:
            orders.append(_order_dict(plan, "blocked", "market_ok=false"))
            continue
        bar = bars.get(str(plan.get("code")))
        if bar is None:
            orders.append(_order_dict(plan, "rejected", "无行情"))
            continue
        trigger = plan["entry"][0]
        current = positions.get(str(plan["code"]), {"qty": 0})
        qty = max(int(initial_equity * pct / trigger / 100) * 100 - current["qty"], 0)
        if not qty:
            continue
        fill, _, _ = simulate_entry(plan, qty, bar, slippage_rate=slippage_rate)
        if not fill.filled:
            orders.append(_order_dict(plan, fill.status, fill.reject_reason))
            continue
        affordable = int(buy_cash / (fill.price * (1 + 0.001)) / 100) * 100
        if affordable < fill.qty:
            fill, _, _ = simulate_entry(plan, affordable, bar, slippage_rate=slippage_rate)
        if not fill.filled or fill.amount + fill.fee > buy_cash:
            orders.append(_order_dict(plan, "rejected", "现金不足"))
            continue
        cash -= fill.amount + fill.fee
        buy_cash -= fill.amount + fill.fee
        positions[str(plan["code"])] = {"qty": current["qty"] + fill.qty, "buy_day": plan_doc.get("date")}
        orders.append(_order_dict(plan, "filled", fill=fill))

    equity = portfolio_value(cash, positions, bars)
    return {
        "initial_cash": initial_cash,
        "initial_equity": initial_equity,
        "positions": positions,
        "cash": round(cash, 2),
        "equity": round(equity, 2),
        "return": round(equity / initial_equity - 1, 6),
        "orders": orders,
    }


def build_counterfactual(plan_doc: dict, bars: dict[str, Bar],
                         initial_cash: float = 100_000,
                         slippage_rate: float = 0.002, *, initial_positions: dict | None = None) -> dict:
    benchmark = bars.get("000300")
    benchmark_return = None
    risk = risk_review_summary(plan_doc)
    if benchmark and benchmark.prev_close and benchmark.close is not None:
        benchmark_return = round(benchmark.close / benchmark.prev_close - 1, 6)
    return {
        "plan_date": plan_doc.get("date"),
        "data_asof": plan_doc.get("data_asof"),
        "sample_type": (
            "forward_eligible"
            if str(plan_doc.get("data_asof") or "") < str(plan_doc.get("date") or "")
            else "same_day_retrospective"
        ),
        "plan_hash": canonical_plan_hash(plan_doc),
        "risk_review": risk,
        "risk_scope": ("validated_decisions" if risk["status"] == "validated_contract"
                       else "unreviewed_hypothesis_no_auto_paper"),
        "evidence_kind": "DAILY_BAR_PROXY",
        "opening_state": {"cash": initial_cash, "positions": deepcopy(initial_positions or {}),
                          "scope": "explicit_seed" if initial_positions else "daily_cash_reset"},
        "execution_model": {
            "version": EXECUTION_MODEL_VERSION,
            "entry_contract": ENTRY_CONTRACT_VERSION,
            "exit_contract": EXIT_CONTRACT_VERSION,
            "portfolio_policy": PORTFOLIO_POLICY_VERSION,
            "inventory_policy": INVENTORY_POLICY_VERSION,
            "sale_proceeds_reuse": "next_session",
            "signal_to_fill": "T+1",
            "slippage_rate": slippage_rate,
            "board_lot": 100,
            "limit_touch": "daily_ohlc_strict",
            "fees": "commission+stamp_tax+transfer_fee",
        },
        "portfolios": {
            "gate": _portfolio(plan_doc, bars, initial_cash, True, slippage_rate, initial_positions or {}),
            "no_gate": _portfolio(plan_doc, bars, initial_cash, False, slippage_rate, initial_positions or {}),
            "csi300": {"return": benchmark_return},
        },
        "bars": {code: asdict(bar) for code, bar in bars.items()},
    }


def _pct(value) -> str:
    return "—" if value is None else f"{value:.2%}"


def render_markdown(report: dict) -> str:
    gate = report["portfolios"]["gate"]
    no_gate = report["portfolios"]["no_gate"]
    money = lambda value: "未知" if value is None else f"{value:,.2f}"
    lines = [
        f"# {report['plan_date']} 严格成交重放与反事实",
        "",
        "> 日线 OHLC 撮合模拟，不是交易所成交回报。",
        "",
        "| 组合 | 收益 | 期末权益 |",
        "|---|---:|---:|",
        f"| 闸门组合 | {_pct(gate['return'])} | {money(gate['equity'])} |",
        f"| 无闸门组合 | {_pct(no_gate['return'])} | {money(no_gate['equity'])} |",
        f"| 沪深300 | {_pct(report['portfolios']['csi300']['return'])} | — |",
        "", "## 严格成交结果", "",
        "| 标的 | 闸门 | 无闸门 | 成交价/原因 |",
        "|---|---|---|---|",
    ]
    evidence = (
        f"> 行情契约：{SCHEMA}；未复权行情缓存哈希：`{report['market_data_hash']}`。"
        if report.get("market_data_schema") == SCHEMA and report.get("market_data_hash")
        else "> 输入行情未验证来源，不可作为已核验的成交证据。"
    )
    lines[4:4] = [evidence, ""]
    risk = report.get("risk_review", {})
    lines[4:4] = [(
        f"> 风控契约：{risk['contract']}，审核 {risk['reviewed_count']} 项；仅闸门否决 {risk['gate_only_count']} 项，其他风险否决 {risk['risk_rejected_count']} 项。"
        if risk.get("status") == "validated_contract" else
        "> 历史风控结构化证据缺失：这里只重放假设意图，不能据此批准自动 Paper，也不补签历史审核。"
    ), ""]
    scope = report.get("opening_state", {}).get("scope")
    lines[4:4] = [(
        "> 初始状态：显式提供的现金与持仓；两组合从同一起点分别重放。"
        if scope == "explicit_seed" else
        "> 初始状态：每日现金重置、零持仓实验，不是连续 Paper 账户收益。"
    ), ""]
    if report.get("sample_type") == "same_day_retrospective":
        lines[3:3] = [
            "> **样本标签：同日事后诊断。** 计划使用了执行日收盘数据，不计入前向 Paper 业绩，也不得倒填成交。",
            "",
        ]
    review = report.get("review_gate")
    if review is not None:
        lines[3:3] = [
            ("> 生成时独立审阅已通过当前计划版本；本表仍只是日线模拟，不是 Paper 成交。"
             if review["passed"] else
             "> 生成时独立审阅未通过或来源失效；以下仅为未获准计划的非执行诊断，不计入 Paper 业绩。"),
            "",
        ]
    gate_by_code = {o["code"]: o for o in gate["orders"]}
    for order in no_gate["orders"]:
        g = gate_by_code.get(order["code"], {})
        detail = (f"{order.get('fill_price'):.3f}, fee={order.get('fee'):.2f}"
                  if order["status"] == "filled" else order.get("reason", ""))
        lines.append(
            f"| {order['name']} {order['code']} | {g.get('status', '—')} | "
            f"{order['status']} | {detail} |"
        )
    lines += [
        "", "## 口径约束", "",
        "- `unfilled` 表示开盘条件未满足或当日 OHLC 未触发入场/退出价，不虚构成交或机会成本；已有持仓继续估值。",
        "- `blocked` 表示市场闸门或执行契约阻止；无闸门组合只绕过市场闸门，不绕过执行契约。",
        "- 无显式退出意图则保留持仓；退出仅为 DAY 市价/限价/止损，买入 stop 字段不是保护单。",
        "- 日线无法确定盘中交易顺序，因此当日卖出所得不用于当日买入；这不是市场资金规则。",
        "- 沪深300 使用执行日收盘相对前收的日收益。",
        "- 日线触价不能证明排队成交；本结果为 DAILY_BAR_PROXY，不作为实盘盈利证据。",
    ]
    return "\n".join(lines) + "\n"


def write_counterfactual(day: str, team: Path = TEAM) -> dict:
    with state_lock(team):
        return _write_counterfactual(day, team)


def _write_counterfactual(day: str, team: Path) -> dict:
    plan_path = team / "outputs" / day / "plan.json"
    plan_doc = json.loads(plan_path.read_text(encoding="utf-8"))
    codes = [str(p["code"]) for p in select_paper_plans(plan_doc)]
    bars = load_execution_bars(day, codes, team)
    document = _cache_document(day, team)
    if document is None or any(
        asdict(bar) != asdict(_bar_from_row(code, document["bars"].get(code), day))
        for code, bar in bars.items()
    ):
        raise ValueError("execution cache changed while building counterfactual")
    report = build_counterfactual(plan_doc, bars)
    report.update(market_data_schema=SCHEMA, market_data_hash=document["payload_hash"])
    from trading_team.multica_reviews import review_status
    review = review_status(day, team=team)
    matched = review.get("source", {}).get("plan_hash") == report["plan_hash"]
    report["review_gate"] = {"status": review["status"], "plan_hash_matched": matched,
                             "passed": review["status"] == "accepted" and matched}
    from trading_team.continuous_counterfactual import write_series
    from trading_team.execution_bars import DEFAULT_TRADING_CALENDAR
    from trading_team.paper_pipeline import PAPER_AUTO_START
    continuous = write_series(day, start=min(PAPER_AUTO_START, day), team=team,
                              calendar_path=DEFAULT_TRADING_CALENDAR)
    report["continuous_report_hash"] = continuous["report_hash"]
    out = team / "outputs" / day
    atomic_write_json(out / "counterfactual.json", report)
    atomic_write_text(out / "counterfactual.md", render_markdown(report))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("day")
    args = parser.parse_args()
    report = write_counterfactual(args.day)
    print(f"{args.day}: gate={report['portfolios']['gate']['return']:.2%}, "
          f"no_gate={report['portfolios']['no_gate']['return']:.2%}, "
          f"CSI300={report['portfolios']['csi300']['return']:.2%}")


if __name__ == "__main__":
    main()
