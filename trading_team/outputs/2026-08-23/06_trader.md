# 06 交易员计划 · 2026-08-23（非交易日复盘版，面向 8-24 交易日）

> 依据 05 研究员辩论结论 + 04 技术关键价位制定。本计划为 **trade_proposal（候选计划）**：不会直接进入 Paper 账户，须经老板审批 + schemas.py 确定性风控通过后方可执行。硬约束自查：单票 ≤25%、买入合计 ≤95%、单日买入 ≤5 笔。数据截至 2026-08-21 收盘。

### 结构化计划（与 plan.json proposals[] 一致）

```json
[
  {"code": "601318", "name": "中国平安", "direction": "买入", "position_pct": 0.15, "entry": [53.0, 52.2], "entry_type": "limit_or_pullback", "stop": 50.6, "condition": "高开超过3%不追，等回踩MA5(52.14)区间；T+1 到账后方可卖出", "horizon": "2-4 周", "confidence": 4},
  {"code": "300750", "name": "宁德时代", "direction": "条件买入", "position_pct": 0.1, "entry": [408.6], "entry_type": "breakout", "stop": 388.0, "condition": "仅当放量突破 20 日高 408.6 且回踩不破时执行；缩量突破或高开超 4% 放弃", "horizon": "2-4 周", "confidence": 3},
  {"code": "601899", "name": "紫金矿业", "direction": "条件买入", "position_pct": 0.1, "entry": [33.8, 33.4], "entry_type": "pullback_only", "stop": 32.2, "condition": "只回踩买（MA5 33.4 至 8-21 低点 33.6 区间），不追 35.38 前高突破", "horizon": "2-4 周", "confidence": 3},
  {"code": "600036", "name": "招商银行", "direction": "条件买入", "position_pct": 0.05, "entry": [38.5, 38.0], "entry_type": "limit_or_pullback", "stop": 37.3, "condition": "中报披露前轻仓试错，仓位即风险预算；放量过 39.15 可升级为标准仓", "horizon": "1-3 周", "confidence": 2},
  {"code": "000858", "name": "五粮液", "direction": "观望", "position_pct": 0.0, "entry": null, "stop": null, "condition": "基本面反转与中报复核确认前不介入；技术上需先收复 MA5 72.1", "horizon": "—", "confidence": 2},
  {"code": "000333", "name": "美的集团", "direction": "观望", "position_pct": 0.0, "entry": null, "stop": null, "condition": "82.4-85.1 整理区间未破前无操作价值", "horizon": "—", "confidence": 2},
  {"code": "600900", "name": "长江电力", "direction": "观望", "position_pct": 0.0, "entry": null, "stop": null, "condition": "防御配置需等 MA60(27.44) 企稳信号；当前缩量阴跌不接", "horizon": "—", "confidence": 2},
  {"code": "600519", "name": "贵州茅台", "direction": "回避", "position_pct": 0.0, "entry": null, "stop": null, "condition": "MA60(1262.6) 失守则下行空间打开；估值派左侧理由不构成本团队买点", "horizon": "—", "confidence": 3},
  {"code": "601012", "name": "隆基绿能", "direction": "回避", "position_pct": 0.0, "entry": null, "stop": null, "condition": "负毛利+连亏，行业出清完成前不左侧", "horizon": "—", "confidence": 3},
  {"code": "000063", "name": "中兴通讯", "direction": "回避", "position_pct": 0.0, "entry": null, "stop": null, "condition": "中报利润腰斩发酵期；33.06 若破看 31.98，不做接刀", "horizon": "—", "confidence": 4},
  {"code": "002594", "name": "比亚迪", "direction": "回避", "position_pct": 0.0, "entry": null, "stop": null, "condition": "净利率 2.67% 价格战未止，横盘企稳的技术证据不足以对抗基本面恶化", "horizon": "—", "confidence": 2}
]
```

### 计划说明

- **主动进攻仅平安 15%**：唯一四角色同向（基本面+2/情绪升温/技术看多/研究信心 4）的标的；分批入场 53.0（市价区）+ 52.2（回踩 MA5），止损 50.6 放在 MA60（51.85）与 8-18 低点（51.17）下方，对应回撤约 -5.2%。
- **两笔条件单不占主动仓位**：宁德只在放量过 408.6 时触发（突破 408.6 入场、止损 388，对应 -5.0%）；紫金只回踩 33.4-33.8 买（止损 32.2 在 MA20 32.87 下方，对应约 -4.7%），不追短线 +8.2% 的热股。
- **招行 5% 轻仓试错**：信心度 2，按纪律不超过半仓试探，5% 即其风险预算；中报披露是加仓/撤退的分界线。
- **硬约束自查**：买入合计 40% ≤ 95%；单票最大 15% ≤ 25%；主动+条件单合计 4 笔 ≤ 5 笔。均合规（最终以 schemas.py 确定性校验为准）。

### 交易员每日结论

1. **结构市里只做最确定的**：指数弱反弹背景下，主动仓位集中平安一处，其余以条件单等待确认，买入合计控制在 40%。
2. **两个触发价位**：宁德 408.6（放量突破）与紫金 33.4-33.8（回踩 MA5），不满足条件一分钱不投。
3. **回避名单比买入名单更重要**：中兴（中报发酵首日）、隆基（负毛利）、茅台（死叉扩大）、比亚迪（净利率 2.7%）——周一任何反弹都不追。
