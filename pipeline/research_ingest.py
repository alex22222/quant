# -*- coding: utf-8 -*-
"""研究线索 → 策略草案流水线（quant-wiki 建议 4，最小可用版）

打通 quant-x-monitor（X/量化博主监控）→ 策略状态机：

1. 读取 quant-x-monitor/reports/<date>.json 条目（platform/author/title/url/summary）
2. 关键词分类（参照文 3 的句子分类器）：trading_signal / risk_management / 不相关
3. 命中 trading_signal 的条目 → 按关键词选型生成 RQAlpha 策略**草案骨架**
   （统一信号接口 PARAMS/FAMILY/generate_targets + rqalpha 月度入口），
   写入 strategies/ingest_<模板>_<hash>.py，由 stage_strategy 自动注册为 research
4. 去重：pipeline/research_ingest_ledger.jsonl 按 URL 记账，幂等

纪律：
- 草案只是模板化骨架（关键词→参数化模板），不是 LLM 自动写的策略；
  晋级 candidate 必须人工核实逻辑并走 pipeline.promote --differentiation（建议 6 门禁）
- 每次运行草案生成数量封顶（MAX_DRAFTS_PER_RUN），防止线索洪泛淹没注册表
- fail closed：报告文件缺失/损坏只记录原因，不中断、不猜数据

用法：
  python -m pipeline.research_ingest --date 2026-08-21
  python -m pipeline.research_ingest --all
  python -m pipeline.research_ingest --all --dry-run   # 只分类不落盘
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "quant-x-monitor" / "reports"
STRAT_DIR = ROOT / "strategies"
LEDGER = ROOT / "pipeline" / "research_ingest_ledger.jsonl"
TZ = timezone(timedelta(hours=8))

MAX_DRAFTS_PER_RUN = 5

# 文 3 的两类关键词分类器（最小可用：中文关键词集合，命中即归类）
SIGNAL_KEYWORDS = {
    "动量", "轮动", "趋势", "均线", "金叉", "死叉", "突破", "通道", "海龟",
    "rsi", "超买", "超卖", "反弹", "均值回归", "反转", "macd", "布林带",
    "止损", "止盈", "择时", "选股", "因子", "策略", "回测", "信号", "量化",
}
RISK_KEYWORDS = {
    "风控", "风险", "回撤", "爆仓", "杠杆", "仓位管理", "黑天鹅", "流动性",
    "波动率", "对冲", "分散",
}
# 明显噪音（币圈/喊单/无信息量）直接跳过，不进分类
NOISE_KEYWORDS = {"转发", "RT @", "直播", "简历"}

# 关键词 → 草案模板选型（按优先级从上到下匹配）
TEMPLATE_RULES = [
    ("mean_reversion", {"rsi", "超买", "超卖", "反弹", "均值回归", "反转"}),
    ("breakout", {"突破", "通道", "海龟", "新高"}),
    ("ma_cross", {"均线", "金叉", "死叉", "macd"}),
    ("momentum", {"动量", "轮动", "趋势", "相对强弱"}),
]

_UNIVERSE = [
    "600519.XSHG", "000858.XSHE", "600036.XSHG", "601318.XSHG", "300750.XSHE",
    "002594.XSHE", "601899.XSHG", "000333.XSHE", "600900.XSHG", "601012.XSHG",
]


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def classify_item(item: dict) -> str | None:
    """条目分类：trading_signal / risk_management / None（噪音或不相关）。"""
    text = f"{item.get('title') or ''} {item.get('summary') or ''}".lower()
    if not text.strip() or any(n.lower() in text for n in NOISE_KEYWORDS):
        return None
    if any(k in text for k in SIGNAL_KEYWORDS):
        return "trading_signal"
    if any(k in text for k in RISK_KEYWORDS):
        return "risk_management"
    return None


def pick_template(item: dict) -> tuple[str, list[str]]:
    """trading_signal 条目 → (模板名, 命中关键词)。无特定命中时用 momentum 兜底。"""
    text = f"{item.get('title') or ''} {item.get('summary') or ''}".lower()
    for name, keys in TEMPLATE_RULES:
        hit = [k for k in keys if k in text]
        if hit:
            return name, hit
    return "momentum", []


# ─────────────────────────────────────────────────────────────
# 草案模板（统一信号接口，月度调仓 + 120 日线大盘风控，rqalpha 可冒烟）
# ─────────────────────────────────────────────────────────────

_TEMPLATE = '''# -*- coding: utf-8 -*-
"""研究草案（{template}）：{title}

来源：quant-x-monitor {date} · {author} · {url}
分类：trading_signal（关键词命中：{keywords}）

⚠️ 本文件由 pipeline/research_ingest.py 自动生成的**草案骨架**：
   信号核心为模板默认实现，参数未经核实。晋级 candidate 前必须人工核实
   逻辑与参数，并走 pipeline.promote --differentiation 说明与在库策略的
   区分度（AGENT.md 工作纪律第 7 条）。
"""
import os
import sys

import numpy as np

_ROOT = os.environ.get("QUANT_ROOT", str(__import__("pathlib").Path.cwd()))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from strategies.base import SignalResult

FAMILY = "ingest_{template}"

PARAMS = {{
    "universe": {universe},
    "momentum_days": 20,
    "hold_num": 3,
    "benchmark": "000300.XSHG",
    "ma_days": 120,
    # TODO(人工)：按来源线索核实/补充参数与信号逻辑
}}


def compute_targets(closes_map, index_close, params):
    """{template} 模板默认实现（草案，待人工核实）。"""
    ma_days = params["ma_days"]
    market_ok = bool(
        index_close is not None and len(index_close) >= ma_days
        and index_close[-1] > index_close[-ma_days:].mean()
    )
    mom_days = params["momentum_days"]
    table = []
    for code, c in closes_map.items():
        if c is None or len(c) < mom_days + 1 or c[-mom_days - 1] <= 0:
            continue
        table.append((code, float(c[-1] / c[-mom_days - 1] - 1.0)))
    table.sort(key=lambda x: -x[1])
    targets = [s for s, _ in table[: params["hold_num"]]] if market_ok else []
    return SignalResult(targets=targets, market_ok=market_ok,
                        detail={{"draft": True, "template": "{template}"}})


def generate_targets(data, params=None, positions=None):
    p = dict(PARAMS)
    if params:
        p.update(params)
    closes_map = {{}}
    for code in p["universe"]:
        if data.is_suspended(code):
            continue
        closes_map[code] = data.closes(code, p["momentum_days"] + 1)
    index_close = data.index_closes(p["benchmark"], p["ma_days"])
    return compute_targets(closes_map, index_close, p)


# ── rqalpha 回测入口（与 compute_targets 同一信号核心）──

def init(context):
    from rqalpha.api import scheduler
    context.params = dict(PARAMS)
    context.stocks = context.params["universe"]
    scheduler.run_monthly(rebalance, tradingday=1)


def rebalance(context, bar_dict):
    from rqalpha.api import history_bars, is_suspended, logger
    p = context.params
    closes_map = {{}}
    for s in p["universe"]:
        if is_suspended(s):
            continue
        h = history_bars(s, p["momentum_days"] + 1, "1d", "close")
        closes_map[s] = (np.asarray(h, dtype=float)
                         if h is not None and len(h) >= p["momentum_days"] + 1 else None)
    idx = history_bars(p["benchmark"], p["ma_days"], "1d", "close")
    idx = np.asarray(idx, dtype=float) if idx is not None and len(idx) >= p["ma_days"] else None
    result = compute_targets(closes_map, idx, p)
    weight = 0.96 / len(result.targets) if result.targets else 0.0
    context.pending = (list(result.targets), weight)
    logger.info(f"草案信号: {{result.targets}} market_ok={{result.market_ok}}")


def open_auction(context, bar_dict):
    pending = getattr(context, "pending", None)
    if not pending:
        return
    context.pending = None
    targets, weight = pending
    from rqalpha.api import get_positions, order_target_percent
    for pos in get_positions():
        if pos.order_book_id not in targets:
            order_target_percent(pos.order_book_id, 0)
    for s in targets:
        order_target_percent(s, weight)


def handle_bar(context, bar_dict):
    pass
'''


def _draft_name(template: str, url: str) -> str:
    h = hashlib.sha256((url or "").encode()).hexdigest()[:6]
    return f"ingest_{template}_{h}"


def _load_ledger_urls(ledger_path: Path) -> set[str]:
    urls = set()
    if ledger_path.exists():
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                u = json.loads(line).get("url")
                if u:
                    urls.add(u)
            except json.JSONDecodeError:
                continue
    return urls


def make_draft(item: dict, date: str, strat_dir: Path) -> str:
    """生成草案文件，返回策略名。"""
    template, hits = pick_template(item)
    name = _draft_name(template, item.get("url") or item.get("title") or "")
    title = re.sub(r"\s+", " ", (item.get("title") or "无标题"))[:60]
    code = _TEMPLATE.format(
        template=template,
        title=title,
        date=date,
        author=item.get("author") or "未知",
        url=item.get("url") or "",
        keywords="、".join(hits) or "（无特定命中，momentum 兜底）",
        universe=json.dumps(_UNIVERSE, ensure_ascii=False, indent=8),
    )
    (strat_dir / f"{name}.py").write_text(code, encoding="utf-8")
    return name


def ingest(date: str | None = None, reports_dir: Path = REPORTS_DIR,
           strat_dir: Path = STRAT_DIR, ledger_path: Path = LEDGER,
           dry_run: bool = False) -> dict:
    """处理报告条目：分类 + 草案生成。date=None 表示全部报告文件。"""
    if date:
        files = [reports_dir / f"{date}.json"]
        if not files[0].exists():
            return {"processed": 0, "drafts": [], "classified": {},
                    "reason": f"报告不存在: {files[0].name}"}
    else:
        files = sorted(reports_dir.glob("*.json"))
        if not files:
            return {"processed": 0, "drafts": [], "classified": {},
                    "reason": f"报告目录为空: {reports_dir}"}

    seen = _load_ledger_urls(ledger_path)
    ledger_lines, drafts = [], []
    classified = {"trading_signal": 0, "risk_management": 0, "ignored": 0}
    processed = 0

    for f in files:
        day = f.stem
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue  # fail closed：坏文件跳过
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            if url and url in seen:
                continue  # 幂等：已记账
            processed += 1
            cls = classify_item(item)
            draft_name = None
            if cls == "trading_signal":
                classified["trading_signal"] += 1
                if not dry_run and len(drafts) < MAX_DRAFTS_PER_RUN:
                    draft_name = make_draft(item, day, strat_dir)
                    drafts.append(draft_name)
            elif cls == "risk_management":
                classified["risk_management"] += 1
            else:
                classified["ignored"] += 1
            ledger_lines.append(json.dumps({
                "url": url, "date": day, "author": item.get("author"),
                "title": (item.get("title") or "")[:80],
                "classification": cls, "draft": draft_name,
                "ingested_at": _now(),
            }, ensure_ascii=False))
            if url:
                seen.add(url)

    if not dry_run and ledger_lines:
        with ledger_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(ledger_lines) + "\n")
    return {"processed": processed, "classified": classified, "drafts": drafts,
            "dry_run": dry_run,
            "hint": "草案已由 stage_strategy 注册为 research；晋级须人工核实 + "
                    "pipeline.promote --differentiation"}


def main():
    ap = argparse.ArgumentParser(prog="pipeline.research_ingest")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--date", help="处理单日报告 YYYY-MM-DD")
    g.add_argument("--all", action="store_true", help="处理全部报告")
    ap.add_argument("--dry-run", action="store_true", help="只分类不落盘")
    args = ap.parse_args()
    out = ingest(date=args.date, dry_run=args.dry_run)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
