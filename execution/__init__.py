# -*- coding: utf-8 -*-
"""execution —— A 股实盘执行层（HQCapital 交易台·实盘线）

分层：
  signals       目标仓位信号（与 paper 线同一策略逻辑，数据源为本地 bundle）
  order_manager 目标仓位 → 具体订单（整手、T+1 可用、涨跌停拦截）
  risk_guard    下单前风控门禁（仓位上限/价格 sanity/日亏损熔断）
  broker_*      券商通道（mock=模拟成交；easytrader=同花顺客户端自动化，Windows）
  ledger        SQLite 台账（订单/成交/每日权益），事实源之一
  notifier      飞书推送（复用 quant-x-monitor 凭证，凭证不落日志）
  runner        每日执行主循环，CLI 入口

纪律（继承 AGENT.md）：
  - 默认 dry-run；live 需 config.json 中 live_enabled=true 且 CLI 显式 --mode live
  - 凭证永不入库、不进日志
"""
