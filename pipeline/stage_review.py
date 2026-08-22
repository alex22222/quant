# -*- coding: utf-8 -*-
"""阶段5·投委会：生成复盘报告 + 向 AGENT.md 追加迭代日志"""
from datetime import date
from pathlib import Path

from .common import ROOT, load_status, now

AGENT_MD = ROOT / "AGENT.md"
REVIEWS = ROOT / "reviews"


def main():
    st = load_status()
    stages = st.get("stages", {})
    today = date.today().isoformat()
    REVIEWS.mkdir(exist_ok=True)

    lines = [f"# 复盘报告 {today}", ""]
    for name in ["data", "strategy", "backtest", "paper"]:
        s = stages.get(name, {})
        status = s.get("status", "未执行")
        lines.append(f"## {name} — {status}（{s.get('elapsed_sec', '-')}s）")
        r = s.get("result", {})
        if name == "data" and r:
            lines.append(f"- 数据 {r.get('stocks')} 只，最新交易日 {r.get('latest_trading_day')}，"
                         f"新鲜度 {r.get('freshness_days')} 天")
        elif name == "strategy" and r:
            bs = r.get("by_state", {})
            lines.append(f"- 注册 {r.get('total')} 个策略：live={bs.get('live')}, "
                         f"candidate={bs.get('candidate')}, research={bs.get('research')}")
        elif name == "backtest" and r:
            lines.append(f"- 门禁版本 {r.get('gate_version', 'v1')}；成本口径：{r.get('cost_model', '-')}")
            for n, res in r.get("results", {}).items():
                if res.get("gate") == "error":
                    lines.append(f"- {n}: 回测失败 {res.get('error', '')[:80]}")
                    continue
                fam = res.get("family", {})
                dup = f"｜族 {fam.get('family')} 代表={fam.get('family_representative')}" \
                    if fam.get("family_dup") else ""
                lines.append(f"- {n}: 年化 {res['annual']:.1%} 回撤 {res['max_dd']:.1%} "
                             f"夏普 {res['sharpe']:.2f} 超额 {res.get('excess_annual', 0):.1%} "
                             f"IR {res.get('ir', 0):.2f} → 门禁 **{res['gate']}**{dup}")
                if res.get("worst_month") is not None:
                    lines.append(f"  - 月度: 最差 {res['worst_month']:.1%} 最好 {res.get('best_month', 0):.1%} "
                                 f"胜率 {res.get('monthly_win_rate', 0):.0%}（{res.get('months', 0)} 个月）")
                # 失败样本必须展示原因（改进计划 Phase 2）
                for reason in res.get("reasons", []):
                    lines.append(f"  - ❌ {reason}")
                oos = res.get("oos")
                if oos:
                    lines.append(f"  - 样本外: 年化 {oos['annual']:.1%} 回撤 {oos['max_dd']:.1%} "
                                 f"夏普 {oos['sharpe']:.2f} 超额 {oos.get('excess_annual', 0):.1%}")
                for p in res.get("perturbations", []):
                    lines.append(f"  - 扰动 {p['variant']}: 年化 {p['annual']:.1%} "
                                 f"回撤 {p['max_dd']:.1%} 夏普 {p['sharpe']:.2f}")
                for sw in res.get("stage_windows", []):
                    lines.append(f"  - 阶段 {sw['window']}: 年化 {sw['annual']:.1%} "
                                 f"回撤 {sw['max_dd']:.1%} 夏普 {sw['sharpe']:.2f} "
                                 f"超额 {(sw.get('excess_annual') or 0):.1%}")
        elif name == "paper" and r:
            lines.append(f"- 记账日 {r.get('day')}（{r.get('note')}）：净值 {r.get('equity')}，"
                         f"累计 {r.get('cum_return', 0):.1%}，持仓 {r.get('positions')}")
            lines.append(f"- live 策略 {r.get('live_strategies')}"
                         f"（族 {r.get('strategy_family')}），market_ok={r.get('market_ok')}")
            for t in r.get("fills", []):
                lines.append(f"  - {t['side']} {t['code']} x{t['qty']} @{t['price']}"
                             f" 费 {t.get('fee', 0)}（{t['reason']}）")
            for o in r.get("pending_orders", []):
                lines.append(f"  - 挂单待成交: {o['side']} {o['code']} x{o['qty']}"
                             f" 计划价 {o.get('planned_price')}")
            lines.append(f"- 假设：{r.get('assumption', '-')}")
        if s.get("error"):
            lines.append(f"- ❌ {s['error']}")
        lines.append("")

    lines.append("## 下一步")
    lines.append("- [ ] 人工审阅本报告后，在 AGENT.md 本条目中补充决策（参数调整 / 策略升降级）")
    lines.append("- [ ] 次月数据包更新后重跑全流程")

    report_path = REVIEWS / f"{today}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")

    # 追加 AGENT.md 迭代日志（只追加，不改旧条目）
    loop_no = 1
    if AGENT_MD.exists():
        import re
        nos = re.findall(r"## Loop #(\d+)", AGENT_MD.read_text(encoding="utf-8"))
        loop_no = int(nos[-1]) + 1 if nos else 1
    entry = f"""
## Loop #{loop_no} — {today}

- **执行**: `pipeline.run` 全流程
- **结果**: 数据新鲜度 {stages.get('data', {}).get('result', {}).get('freshness_days', '?')} 天；
  paper 净值 {stages.get('paper', {}).get('result', {}).get('equity', '?')}；
  回测门禁 {[(k, v.get('gate')) for k, v in stages.get('backtest', {}).get('result', {}).get('results', {}).items()]}
- **报告**: reviews/{today}.md
- **人工决策**: （待填写）
- **下一步**: （待填写）
"""
    with AGENT_MD.open("a", encoding="utf-8") as f:
        f.write(entry)

    return {"report": f"reviews/{today}.md", "loop_no": loop_no,
            "agent_md_updated": True}
