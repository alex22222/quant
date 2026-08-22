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

## Loop #5 — 2026-08-22

- **执行**: 新建 `trading_team/` —— TradingAgents 七角色交易团队 v1（基本面/情绪/新闻/技术分析师 + 研究员/交易员/风险经理），首日全流程运行
- **内容**:
  - 角色层：`prompts/01~07` 七份角色章程（职责/输入/纪律/输出格式），观察池 5 只（茅台/招行/平安/宁德/中兴）+ 沪深300 基准
  - 数据层：`collect.py` 确定性采集 → `context/<date>/`（快照/120日K线+MA/MACD/RSI/F10财务/公告/快讯/涨跌停池），东财主源 + 腾讯兜底，单源失败记入 manifest 不中断
  - 首日日报：`outputs/2026-08-22/01~07` + `daily_conclusion.md`（数据截至 8-21 收盘）
  - 准确率追踪：`loops/accuracy.json` 建档，T+5 对照计分，角色 <50% 自动降权（风控提出的制度性要求）
- **结果**: 采集 13/13 源 OK；首日结论——看多平安(信心4)/回避中兴(信心4)，风控放行组合最坏情形 -1.55%
- **发现**: 东财 push2/push2his 接口有偶发代理抖动，腾讯兜底机制有效；市场宽度统计 filter 未生效（数据缺口，待修）；观察池当日无市值加权口径
- **人工决策**: （待填写）
- **下一步**: ① 修复市场宽度统计；② 建每日定时任务（盘前采集+角色流水线）；③ T+5 后跑首次准确率验证；④ 观察池扩至 momentum_rotation 全池 10 只

## Loop #5 — 2026-08-22

- **执行**: TradingAgents 多智能体框架落地（Phase 3 启动，晨会制）
- **内容**:
  - 数据入口：`trading_team/briefing.py` 晨会数据包生成器（观察池技术快照 + 120 日线风控前置 + paper 账户 + 近期情报），确定性代码，LLM 唯一数据源
  - 晨会 Automation「晨会 · LLM 交易团队」：每周六 10:17（Asia/Shanghai）自动召开，依次扮演分析师×4 → 多空研究员 → 交易员 → 风控经理，产出 `decisions/YYYY-MM-DD/`（meeting.md + decision.json + index.json）
  - 控制台新增「🤖 晨会 · LLM团队」菜单：最新结论、PM 审批状态、交易计划表、晨会纪要、历史列表
  - 纪律：LLM 只出提案（pm_status=pending），不动 paper 台账；审批权在老板
- **实测**: 手动触发首跑成功——沪深300 跌破 120 日线，LLM 团队全票观望（5/5），与确定性风控结论一致；风控经理还识别出数据包三周时滞并要求刷新数据后再评估开仓
- **人工决策**: （待填写——首份晨会提案待老板审批）
- **下一步**: 老板在「人工决策」批复首份提案；数据包刷新（8 月 bundle）后重开晨会验证开仓流程
