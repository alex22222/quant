# AGENT.md — Loop Engineering 协议与迭代日志

> 本文件由人（老板）与 Agent 共同维护。机器只追加 Loop 条目，不修改历史；人可编辑任何部分。

## 运作协议

每个迭代（Loop）固定走五步，对应 `pipeline/` 五个阶段：

```
data（数据部门）→ strategy（技术分析师）→ backtest（风控门禁）→ paper（交易员）→ review（投委会）
```

### 角色与职责（模拟投资公司，参考 TradingAgents）

| 角色 | 职责 | 产出 |
|------|------|------|
| 数据部门 | 保证 A 股数据新鲜可用 | status.data |
| 技术分析师 | 维护策略注册表与状态机 research→candidate→live→retired | status.strategy |
| 风控委员会 | 统一区间回测，门禁：年化>5% 且回撤<30% 且夏普>0.3 | status.backtest |
| 交易员 | paper 账户每日记账（SQLite，收盘价成交） | status.paper + paper/paper.db |
| 投委会 | 复盘报告 + 本文件追加 Loop 条目 | reviews/*.md |

### 规则

1. **一条命令一个 Loop**：`.venv/bin/python -m pipeline.run`
2. **门禁不可绕过**：只有 `live` 策略进 paper；状态变更须记录在某个 Loop 条目的"人工决策"里
3. **事实源**：`status.json`（机器写）；`AGENT.md`（共同写）；控制台只读
4. **敏感信息**：凭证永不入库、不进本文件
5. **每次 Loop 结束必须追加下方日志**，字段：执行内容、结果、报告链接、人工决策、下一步

## 当前状态速览

- 上线策略：`momentum_stops`（stop=none 模式，即纯动量轮动 + 120 日线风控）
- 候选策略：`momentum_rotation`（与上线策略逻辑等价，待门禁复核）
- 已知结论：KDE 支撑位无预测 edge；作止损锚压回撤但损夏普（详见 README）

## 迭代日志

（由 review 阶段自动追加 ↓）

## Loop #1 — 2026-08-22

- **执行**: `pipeline.run` 全流程
- **结果**: 数据新鲜度 22 天；
  paper 净值 ?；
  回测门禁 [('momentum_rotation', 'pass'), ('momentum_stops', 'pass')]
- **报告**: reviews/2026-08-22.md
- **人工决策**: （待填写）
- **下一步**: （待填写）

## Loop #2 — 2026-08-22

- **执行**: `pipeline.run` 全流程
- **结果**: 数据新鲜度 22 天；
  paper 净值 100000.0；
  回测门禁 [('momentum_rotation', 'pass'), ('momentum_stops', 'pass')]
- **报告**: reviews/2026-08-22.md
- **人工决策**: （待填写）
- **下一步**: （待填写）

## Loop #3 — 2026-08-22

- **执行**: `pipeline.run` 全流程
- **结果**: 数据新鲜度 22 天；
  paper 净值 100000.0；
  回测门禁 [('momentum_rotation', 'pass'), ('momentum_stops', 'pass')]
- **报告**: reviews/2026-08-22.md
- **人工决策**: （待填写）
- **下一步**: （待填写）

## Loop #4 — 2026-08-22

- **执行**: 控制台新增「量化新闻」页面（非 pipeline 阶段，属情报线建设）
- **内容**:
  - quant-x-monitor 日报历史回填：`backfill_reports.py` 从飞书群拉取 13 张日报卡片，按 URL 去重存档至 `quant-x-monitor/reports/YYYY-MM-DD.json`（8-10 ~ 8-22 共 12 天、68 条）
  - 每日总结：`console/news/YYYY-MM-DD.md` + `index.json`（agent 撰写，含「要点 / 量化相关性」评级）
  - 控制台侧边栏新增「📰 量化新闻」菜单（总部组，带状态灯），左日期列表 + 右总结正文
  - 每日监控 Automation 提示词更新：推送后自动存档 reports/ 并生成当日 news 总结、重建 index.json
- **发现**: 8-16 前监控配置含孙宇晨/CZ/Musk 等币圈账号，历史日报噪音大；8-16 起切换为量化博主后信噪比显著提升（8-16/8-17 为高量化含量日）
- **人工决策**: （待填写）
- **下一步**: 可考虑把高量化含量日的策略研究线索沉淀为投研部 research 候选
