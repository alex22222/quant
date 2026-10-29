"""accuracy.json 维护脚本（2026-09-20 复盘版 run）
1) 补齐 8-30~9-11 及今日条目的 trade_date_ref / verify_after（T+5 交易日）
2) 对 verify_after <= 2026-09-20 的未验证条目：对照最新 K 线做 5 日涨跌计分
   规则（accuracy.json「说明」字段）：评级方向与 5 日实际涨跌一致得 1 分，相反 0 分，
   中性且 |涨跌|<2% 得 1 分。强烈看多/看多/中性偏多=多方向；中性偏空/看空/强烈看空=空方向。
3) 回填 result_5d / score / verified，并把每期得分追加到 role_accuracy.researcher
"""
import json
from pathlib import Path

TEAM = Path("trading_team")
TODAY = "2026-09-20"
CTX = TEAM / "context" / TODAY

# --- 交易日历：以沪深300 K线为准 ---
idx = json.loads((CTX / "technical_000300.json").read_text())
DAYS = [k["date"] for k in idx["recent_klines"]]

def plus5(d):
    i = DAYS.index(d)
    return DAYS[i + 5] if i + 5 < len(DAYS) else None

# --- 各代码 K 线收盘价表 ---
closes = {}
for p in CTX.glob("technical_*.json"):
    code = p.stem.split("_")[1]
    t = json.loads(p.read_text())
    closes[code] = {k["date"]: k["close"] for k in t.get("recent_klines", [])}

def direction(rating):
    if rating in ("强烈看多", "看多", "中性偏多"):
        return "bull"
    if rating in ("看空", "强烈看空", "中性偏空"):
        return "bear"
    return "neutral"

def score_one(rating, ret):
    d = direction(rating)
    if d == "bull":
        return 1 if ret > 0 else 0
    if d == "bear":
        return 1 if ret < 0 else 0
    return 1 if abs(ret) < 0.02 else 0

# --- 各日报数据基准日（plan.json data_asof） ---
def data_asof(date):
    p = TEAM / "outputs" / date / "plan.json"
    if p.exists():
        return json.loads(p.read_text()).get("data_asof")
    return None

acc_path = TEAM / "loops" / "accuracy.json"
acc = json.loads(acc_path.read_text())

log = []
for e in acc["entries"]:
    # 1) 补齐字段
    if not e.get("trade_date_ref"):
        ref = data_asof(e["date"])
        if ref and ref in DAYS:
            e["trade_date_ref"] = ref
            e["verify_after"] = plus5(ref) or e.get("verify_after")
            log.append(f"{e['date']}: 补齐 ref={ref} verify_after={e['verify_after']}")
        else:
            log.append(f"{e['date']}: 无法补齐（plan.json 缺失或基准日不在日历），跳过")
            continue
    # 2) 到期计分
    va = e.get("verify_after")
    if e.get("verified") or not va or va > TODAY:
        continue
    ref = e["trade_date_ref"]
    result, scores = {}, []
    for code, r in e.get("ratings", {}).items():
        series = closes.get(code, {})
        c0, c1 = series.get(ref), series.get(va)
        if c0 is None or c1 is None:
            log.append(f"{e['date']} {code}: K线缺 {ref}/{va}，跳过该票")
            continue
        ret = c1 / c0 - 1
        result[code] = round(ret * 100, 2)
        s = score_one(r["rating"], ret)
        scores.append(s)
        # 逐票明细挂到 rating 上，便于审计
        r["ret_5d_pct"] = round(ret * 100, 2)
        r["hit"] = s
    if not scores:
        log.append(f"{e['date']}: 无可用 K 线，保持未验证")
        continue
    e["result_5d"] = result
    e["score"] = round(sum(scores) / len(scores), 3)
    e["verified"] = True
    e["scored_at"] = TODAY
    acc["role_accuracy"]["researcher"].append({"date": e["date"], "score": e["score"]})
    log.append(f"{e['date']}: verified score={e['score']} ({sum(scores)}/{len(scores)})")

# 3) 今日条目补 note / trader_plan（backfill 已写入评级）
plan_path = TEAM / "outputs" / TODAY / "plan.json"
for e in acc["entries"]:
    if e["date"] == TODAY:
        e["trade_date_ref"] = "2026-09-18"
        e["verify_after"] = "2026-09-25"  # T+5 交易日按周日历推算；若中秋休市则顺延
        e["note"] = "非交易日复盘版（周日，基于9-18收盘）；闸门连续第五日关闭，5笔条件买入预案全部blocked；冻结整周躲过平安-3.9%/紫金-7.9%/宁德-10.7%"
        if plan_path.exists():
            plan = json.loads(plan_path.read_text())
            e["trader_plan"] = {
                p["code"]: {"action": f"{p['direction']}"
                            + (f"(预案{p['entry']} 止损{p['stop']})" if p.get("entry") else ""),
                            "gate": "条款4冻结" if p.get("entry") else "-"}
                for p in plan["proposals"]
            }
        log.append(f"{TODAY}: 今日条目补齐 ref=2026-09-18 verify_after=2026-09-25")

acc_path.write_text(json.dumps(acc, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n".join(log))
