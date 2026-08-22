# -*- coding: utf-8 -*-
"""runner：每日实盘执行主循环

用法：
  演练（默认，MockBroker 模拟成交）：
    .venv/bin/python -m execution.runner --mode dry-run
  实盘（Windows + 同花顺客户端 + config.json 中 live_enabled=true）：
    python -m execution.runner --mode live

幂等：同一交易日同一 mode 只执行一次，--force 覆盖。
流程：信号 → 行情 → 订单计划 → 风控门禁 → 下单 → 委托对账 → 台账 → 飞书。
"""
import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=8))

from . import market, signals  # noqa: E402
from .broker_base import OrderResult, to_std  # noqa: E402
from .broker_mock import MockBroker  # noqa: E402
from .ledger import Ledger  # noqa: E402
from .notifier import push_card  # noqa: E402
from .order_manager import build_plan  # noqa: E402
from .risk_guard import RiskConfig, check  # noqa: E402


def _now():
    return datetime.now(TZ)


def _in_trading_hours(dt) -> bool:
    if dt.weekday() >= 5:
        return False
    hm = dt.hour * 100 + dt.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def load_config(path: Path) -> dict:
    p = path or (HERE / "config.json")
    if not p.exists():
        example = HERE / "config.example.json"
        print(f"[execution] {p.name} 不存在，按 config.example.json 默认值运行（dry-run）",
              file=sys.stderr)
        return json.loads(example.read_text(encoding="utf-8"))
    return json.loads(p.read_text(encoding="utf-8"))


def make_broker(mode: str, cfg: dict):
    if mode == "live":
        if not cfg.get("live_enabled"):
            raise RuntimeError("live 模式需 config.json 中 live_enabled=true")
        from .broker_easytrader import EasyTraderBroker
        return EasyTraderBroker(cfg.get("xiadan_path") or None)
    return MockBroker(HERE / "mock_broker.db")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="execution.runner")
    ap.add_argument("--mode", choices=["dry-run", "live"], default="dry-run")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--signal", type=Path, default=None,
                    help="目标权重 JSON {code: weight}；缺省用 momentum 策略信号")
    ap.add_argument("--date", default=None, help="台账记账日，缺省今天")
    ap.add_argument("--force", action="store_true", help="忽略幂等/交易时段检查")
    ap.add_argument("--no-push", action="store_true", help="不推飞书")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    day = args.date or _now().date().isoformat()

    if args.mode == "live" and cfg.get("trading_hours_only", True) \
            and not _in_trading_hours(_now()) and not args.force:
        return {"status": "skipped", "reason": "非交易时段（--force 可覆盖）"}

    ledger = Ledger(HERE / "execution.db")
    if ledger.has_run(day, args.mode) and not args.force:
        return {"status": "skipped", "reason": f"{day} {args.mode} 已执行，幂等跳过"}

    # ---- 信号 ----
    if args.signal:
        targets = {k: float(v) for k, v in
                   json.loads(args.signal.read_text(encoding="utf-8")).items()}
        sig_meta = {"source": str(args.signal)}
    else:
        targets, sig_meta = signals.target_weights()
        sig_meta["source"] = "momentum_rotation(bundle)"

    # ---- 通道与行情 ----
    broker = make_broker(args.mode, cfg)
    broker.connect()
    bal = broker.balance()
    pos = broker.positions()
    codes = sorted(set(targets) | {p.code for p in pos})
    px = market.quotes([to_std(c) for c in codes])

    plan = build_plan(targets, bal, pos, px,
                      slippage=cfg.get("slippage", 0.002),
                      min_trade_pct=cfg.get("min_trade_pct", 0.02))

    rc = RiskConfig(**cfg.get("risk", {}))
    guard = check(plan, rc, px, pos,
                  prev_equity=ledger.prev_equity(day),
                  orders_today=ledger.orders_today(day))

    # ---- 执行 ----
    ts = _now().isoformat(timespec="seconds")
    sent, filled = 0, 0
    results = []
    for item in guard.blocked:
        ledger.log_blocked(day, ts, item, args.mode)
        results.append({**item, "status": "blocked"})

    if not guard.halted:
        for o in guard.passed:
            fn = broker.buy if o.side == "buy" else broker.sell
            try:
                r = fn(o.code, o.price, o.qty)
            except Exception as e:
                # 单笔异常不中断整批；记为 error，对账阶段以券商侧为准
                r = OrderResult(ok=False, message=f"{type(e).__name__}: {e}")
            ledger.log_order(day, ts, o, args.mode, r)
            sent += 1
            if r.ok:
                filled += 1
            results.append({"code": o.code, "side": o.side, "qty": o.qty,
                            "price": o.price, "status": "filled" if r.ok else "rejected",
                            "message": r.message, "order_id": r.order_id})
    else:
        results.append({"status": "halted", "reason": guard.halt_reason})

    # ---- 对账与收盘 ----
    entrusts = []
    try:
        entrusts = [e.__dict__ for e in broker.today_entrusts()]
    except Exception as e:
        print(f"[execution] 委托对账失败（不阻断）: {type(e).__name__}: {e}")

    bal2 = broker.balance()
    note = "熔断" if guard.halted else ("无交易" if sent == 0 else "已执行")
    ledger.close_day(day, args.mode, bal2.total_asset, bal2.cash, sent, filled, note)

    summary = {
        "status": "ok", "day": day, "mode": args.mode, "note": note,
        "signal": sig_meta, "equity": round(bal2.total_asset, 2),
        "cash": round(bal2.cash, 2),
        "planned": len(plan.orders), "sent": sent, "filled": filled,
        "blocked": len(guard.blocked), "skipped": plan.skipped,
        "results": results, "entrusts": entrusts,
    }

    # ---- 飞书 ----
    if not args.no_push:
        title = f"{'🔴 实盘' if args.mode == 'live' else '🟡 演练'}执行 | {day}"
        lines = [("信号源", sig_meta.get("source", "-")),
                 ("大盘120日线", "上方 ✅" if sig_meta.get("market_ok_120ma") else "下方 ⛔"),
                 ("权益", f"{bal2.total_asset:,.0f}"),
                 ("计划/已报/已成", f"{len(plan.orders)} / {sent} / {filled}"),
                 ("拦截", f"{len(guard.blocked)} 条"),
                 ("备注", note)]
        if guard.halted:
            lines.append(("熔断", guard.halt_reason))
        push_card(title, lines,
                  color="red" if (args.mode == "live" or guard.halted) else "yellow",
                  config_path=(HERE / cfg["feishu_config"]).resolve()
                  if cfg.get("feishu_config") else None)

    broker.close()
    ledger.close()
    return summary


if __name__ == "__main__":
    out = main()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    sys.exit(0 if out.get("status") in ("ok", "skipped") else 1)
