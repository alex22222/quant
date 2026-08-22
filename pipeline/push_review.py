# -*- coding: utf-8 -*-
"""复盘推送：把最新 reviews/*.md 摘要推送到飞书（复用 quant-x-monitor 的凭证）"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "quant-x-monitor"))
from feishu_pusher import get_feishu_token, send_feishu_text  # noqa: E402

CFG = json.loads((ROOT / "quant-x-monitor" / "config.json").read_text(encoding="utf-8"))["feishu"]


def latest_review():
    files = sorted((ROOT / "reviews").glob("*.md"))
    return files[-1] if files else None


def build_text():
    st = json.loads((ROOT / "status.json").read_text(encoding="utf-8"))
    stages = st.get("stages", {})
    lines = [f"📊 量化闭环 Loop 复盘（{st.get('last_loop_at', '?')[:10]}）", ""]
    for name, label in [("data", "数据"), ("backtest", "回测门禁"), ("paper", "Paper")]:
        s = stages.get(name, {})
        mark = "✅" if s.get("status") == "ok" else "❌"
        r = s.get("result", {})
        if name == "data" and r:
            lines.append(f"{mark} 数据: {r.get('stocks')} 只, 最新 {r.get('latest_trading_day')}, 新鲜度 {r.get('freshness_days')}d")
        elif name == "backtest" and r:
            for k, v in r.get("results", {}).items():
                if v.get("gate") != "error":
                    lines.append(f"{mark} 回测 {k}: 年化 {v['annual']:.1%} 回撤 {v['max_dd']:.1%} 夏普 {v['sharpe']:.2f} → {v['gate']}")
        elif name == "paper" and r:
            lines.append(f"{mark} Paper({r.get('day')}): 净值 ¥{r.get('equity'):,.0f} 累计 {r.get('cum_return', 0):.1%} [{r.get('note')}]")
            for t in r.get("trades", []):
                lines.append(f"   {t['side']} {t['code']} x{t['qty']} @{t['price']} ({t['reason']})")
    rev = latest_review()
    if rev:
        lines += ["", f"报告: reviews/{rev.name}", "控制台: http://localhost:7100/console/"]
    return "\n".join(lines)


def push():
    text = build_text()
    token = get_feishu_token(CFG["app_id"], CFG["app_secret"])
    if not token:
        raise RuntimeError("飞书 token 获取失败")
    ok = send_feishu_text(token, CFG["receive_id"], CFG["receive_id_type"], text)
    return {"pushed": ok, "chars": len(text)}


if __name__ == "__main__":
    print(push())
