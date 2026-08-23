# -*- coding: utf-8 -*-
"""Paper 干跑验证（绕过幂等）：复制 paper.db 到临时副本，在其上完整走一遍
「加载 live 策略 → 生成信号 → 生成挂单」，真实台账零改动；跑完删除副本。
用法：.venv/bin/python pipeline/paper_dryrun.py"""
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = ROOT / "paper" / "paper.db"
TMP = ROOT / "paper" / "paper.db.dryrun"

shutil.copy(SRC, TMP)
try:
    import pipeline.stage_paper as sp
    from pipeline.common import load_status
    from strategies.base import BundleDataAdapter

    sp.DB = TMP  # 全部写入只落在副本上
    st = load_status()
    name, mod, params = sp._load_live_strategy(st)
    print(f"[live] {name}  FAMILY={getattr(mod, 'FAMILY', '?')}  "
          f"FREQUENCY={getattr(mod, 'FREQUENCY', '?')}")

    data = BundleDataAdapter()
    latest = data.latest()
    print(f"[data] bundle 最新交易日: {latest}")

    conn = sp._db()
    positions = {r[0]: {"qty": r[1], "cost": r[2], "buy_day": r[3]}
                 for r in conn.execute("SELECT code, qty, avg_cost, buy_day FROM positions")}
    cash = conn.execute(
        "SELECT cash FROM account ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    nav_hist = [e / sp.INIT_CASH for _, e in conn.execute(
        "SELECT day, equity FROM account WHERE day != 'init' ORDER BY day")]
    print(f"[account] cash={cash:.0f}  持仓={list(positions) or '空仓'}  "
          f"nav_hist 长度={len(nav_hist)}")

    # 统一接口信号生成（与 main() 同口径传参）
    signal = mod.generate_targets(data, params, positions=positions, nav_hist=nav_hist)
    print(f"[signal] market_ok={signal.market_ok}  targets={signal.targets}")
    print(f"[signal] detail={json.dumps(signal.detail, ensure_ascii=False, default=str)}")

    # 在副本上生成挂单，验证 _make_rebalance_orders 链路
    n = sp._make_rebalance_orders(
        conn, signal.targets, latest, cash, positions, name,
        params["universe"], data._bd,
        weight_cap=signal.detail.get("weight_cap", params.get("weight_cap", 0.98)))
    orders = conn.execute(
        "SELECT code, side, qty, ROUND(planned_price,2), signal_source "
        "FROM pending_orders WHERE status='pending'").fetchall()
    print(f"[orders] 新挂单 {n} 笔（下一个 bundle 交易日 T+1 开盘撮合）:")
    for o in orders:
        print(f"   {o[1]:<4} {o[0]:<11} {o[2]:>6} 股  计划价 {o[3]}  来源 {o[4]}")
    if not orders:
        print("   （无：目标与现持仓偏离 <2% 或无量价变化，属正常持有状态）")
    conn.close()
finally:
    TMP.unlink(missing_ok=True)
