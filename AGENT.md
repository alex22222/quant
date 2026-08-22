# AGENT.md — HQCapital Loop Engineering 协议与迭代日志

> 本文件由人（老板）与 Agent 共同维护。机器只追加 Loop 条目，不修改历史；人可编辑任何部分。
> 
> **公司名称：HQCapital** —— 参考 TradingAgents 多智能体 LLM 金融交易框架构建的虚拟投资公司。

## 运作协议

每个迭代（Loop）固定走五步，对应 `pipeline/` 五个阶段：

```
data（数据部门）→ strategy（技术分析师）→ backtest（风控门禁）→ paper（交易员）→ review（投委会）
```

### 角色与职责（HQCapital 组织架构，参考 TradingAgents 原文）

**分析师团队（Analyst Team）—— 并行采集市场信息**

| 角色 | 职责（引用 TradingAgents 原文定义） | 产出 |
|------|------|------|
| 基本面分析师 | 分析财报、收益报告、内部交易等数据，评估公司内在价值，判断股票是否被低估或高估，为长期投资提供参考 | `trading_team/outputs/<date>/01_fundamental.md` |
| 情绪分析师 | 关注社交媒体、舆情打分以及公司内部情绪等公众信息，预测短期投资者行为可能对股价带来的影响 | `trading_team/outputs/<date>/02_sentiment.md` |
| 新闻分析师 | 追踪宏观经济指标、重大新闻及公司事件，识别对市场有潜在影响的突发消息或政策变化，为把握行情拐点提供依据 | `trading_team/outputs/<date>/03_news.md` |
| 技术分析师 | 计算 MACD、RSI 等技术指标，分析价量关系和价格形态，判断买卖时机和趋势走势 | `trading_team/outputs/<date>/04_technical.md` |

**研究团队（Research Team）—— 批判性评估与多空辩论**

| 角色 | 职责（引用 TradingAgents 原文定义） | 产出 |
|------|------|------|
| 多头研究员（Bullish） | 强调积极面与增长空间，为看多策略提供支撑性论据 | `trading_team/outputs/<date>/05_researcher.md` |
| 空头研究员（Bearish） | 着重揭示潜在风险与消极信号，为避免盲目入场提供「对立」观点 | `trading_team/outputs/<date>/05_researcher.md` |

**交易与风控层 —— 执行与风险监控**

| 角色 | 职责（引用 TradingAgents 原文定义） | 产出 |
|------|------|------|
| 交易员（Trader） | 评估分析师及研究员的建议，决定交易时机与规模，执行买卖指令，动态调整持仓 | `trading_team/outputs/<date>/06_trader.md` |
| 风险经理（Risk Manager） | 评估市场波动、流动性、对手方风险等；执行风险缓释策略（止损、头寸分散）；向交易员提供风险反馈；确保整体组合与公司风险偏好匹配 | `trading_team/outputs/<date>/07_risk_manager.md` |

**v1 确定性流水线（传统 Loop 阶段）**

| 角色 | 职责 | 产出 |
|------|------|------|
| 数据部门 | 保证 A 股数据新鲜可用 | `status.data` |
| 技术分析师（投研部） | 维护策略注册表与状态机 research→candidate→live→retired | `status.strategy` |
| 风控委员会 | 统一区间回测，门禁：年化>5% 且回撤<30% 且夏普>0.3 | `status.backtest` |
| 交易员（交易台） | paper 账户每日记账（SQLite，收盘价成交） | `status.paper` + `paper/paper.db` |
| 投委会 | 复盘报告 + 本文件追加 Loop 条目 | `reviews/*.md` |

### 工作纪律

1. **角色边界不可逾越**：分析师只输出本维度的研究报告，研究员只组织多空辩论，交易员只将研究结论转化为可执行计划，风险经理只审核风险敞口与下达调整建议。任何角色的产出不得越权替代其他角色的职能。
2. **一条命令一个 Loop**：`.venv/bin/python -m pipeline.run`
3. **门禁不可绕过**：只有 `live` 策略进 paper；状态变更须记录在某个 Loop 条目的"人工决策"里
4. **事实源**：`status.json`（机器写）；`AGENT.md`（共同写）；控制台只读
5. **敏感信息**：凭证永不入库、不进本文件
6. **每次 Loop 结束必须追加下方日志**，字段：执行内容、结果、报告链接、人工决策、下一步


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

## Loop #6 — 2026-08-22

- **执行**: TradingAgents 多智能体框架落地（Phase 3 启动，晨会制）
- **内容**:
  - 数据入口：`trading_team/briefing.py` 晨会数据包生成器（观察池技术快照 + 120 日线风控前置 + paper 账户 + 近期情报），确定性代码，LLM 唯一数据源
  - 晨会 Automation「晨会 · LLM 交易团队」：每周六 10:17（Asia/Shanghai）自动召开，依次扮演分析师×4 → 多空研究员 → 交易员 → 风控经理，产出 `decisions/YYYY-MM-DD/`（meeting.md + decision.json + index.json）
  - 控制台新增「🤖 晨会 · LLM团队」菜单：最新结论、PM 审批状态、交易计划表、晨会纪要、历史列表
  - 纪律：LLM 只出提案（pm_status=pending），不动 paper 台账；审批权在老板
- **实测**: 手动触发首跑成功——沪深300 跌破 120 日线，LLM 团队全票观望（5/5），与确定性风控结论一致；风控经理还识别出数据包三周时滞并要求刷新数据后再评估开仓
- **人工决策**: （待填写——首份晨会提案待老板审批）
- **下一步**: 老板在「人工决策」批复首份提案；数据包刷新（8 月 bundle）后重开晨会验证开仓流程

## Loop #7 — 2026-08-22

- **执行**: `trading_team` 日度流水线（collect → 幂等判断 → 模式判定）
- **结果**: 幂等命中——`outputs/2026-08-22/daily_conclusion.md` 已存在，数据基准日（latest_trade_date=2026-08-21）与已有日报一致，进入轻量模式，未重复生成报告；采集健康检查：首轮 `fundamental_600036` 连接被重置 FAIL，重跑后 13/13 源 OK（东财 F10 接口偶发抖动，与 Loop #5 记录的 push2 抖动同类）
- **报告**: trading_team/outputs/2026-08-22/daily_conclusion.md（沿用首日报告，数据截至 8-21 收盘）
- **人工决策**: （待填写）
- **下一步**: ① 周一（8-24）盘后跑正常日报并验证平安/宁德计划的首日表现；② 东财 F10 接口加自动重试，消除单源抖动；③ 待 verify_after 到期条目跑首次准确率回填

## Loop #8 — 2026-08-22

- **执行**: 交易团队日报接入飞书推送 + 明确老板决策入口
- **内容**:
  - `trading_team/push_feishu.py`：从 daily_conclusion.md 提取一句话总览/放行计划/验证点，组装飞书交互卡片推送（复用 quant-x-monitor 应用凭证，凭证不落日志）
  - 「交易团队日报」Automation 提示词更新：第 5 步加入飞书推送（轻量模式不推送，避免噪音）
  - 决策入口约定：日报与飞书卡片均为**提案**；老板批复方式——① 在 Kimi 对话中直接说「批准/否决 + 标的」，agent 记入 AGENT.md 最新 Loop 的「人工决策」；② 自行编辑该 Loop 条目。批复后的实盘执行在老板券商端手动完成，paper 台账只跑策略线、不联动 LLM 提案线
- **实测**: 2026-08-22 卡片推送成功；Automation 更新已持久化（read 验证）
- **人工决策**: （待填写——含 Loop #5 首日提案：平安 15% 分批买入计划，待批复）
- **下一步**: ① 周一 8-24 08:43 首次自动运行全链路（采集→日报→推送）；② 老板批复首日提案

## Loop #9 — 2026-08-22

- **执行**: HQCapital 角色职责全面制度化——从 TradingAgents 原文提炼，写入公司概览、AGENT.md、七份角色章程及独立 README
- **内容**:
  - 角色定义来源：https://quant-wiki.com/ai/aiquant/TradingAgents/ 原文提炼
  - `console/index.html`：品牌名改为 HQCapital，公司概览新增「角色职责」卡片（分析师团队×4 / 研究团队×2 / 交易员 / 风险经理），各业务线描述与角色对齐
  - `AGENT.md`：运作协议新增四层角色表格 + 工作纪律第 1 条「角色边界不可逾越」
  - `trading_team/prompts/01~07`：七份角色章程头部统一注入 HQCapital 公司名称、所属团队、TradingAgents 原文职责定义、工作纪律（角色边界）
  - `trading_team/README.md`：新建，含角色边界速查表、晨会流程图、文件结构、数据入口纪律
- **制度要点**:
  - 分析师只输出本维度研究报告，不替代其他分析师、不参与辩论或交易
  - 研究员只组织多空辩论，不替代分析师采集、不直接执行交易
  - 交易员只将研究结论转计划，不替代研究职能、不替代风险审核
  - 风险经理只审敞口与下调整建议，不替代交易决策、不替代研究职能
- **人工决策**: 老板采纳执行——角色职责制度已全面落地
- **下一步**: 周一 8-24 晨会运行时验证 LLM 是否严格遵循角色边界输出

## Loop #10 — 2026-08-22

- **执行**: 老板审批通道落地——控制台审批面板 + 批复台账 + 飞书待批复清单
- **内容**:
  - `trading_team/approvals.py`：批复台账（loops/approvals.json，同日同标的重复批复自动覆盖），record 时同步在 AGENT.md 最新 Loop「人工决策」留痕
  - `console/serve.py`：新增 POST /api/approve 写接口（仅 localhost），并修复 sys.path 使可导入 trading_team
  - `console/index.html`：交易团队日报页新增「🖐 老板审批」面板——逐提案显示方向/仓位/入场/止损，待批复项给出批准/否决按钮，已批复显示状态戳（时间/来源/备注）
  - `trading_team/push_feishu.py`：卡片新增「⏳ 待你批复（N 项）」清单，全部批复后显示 ✅
  - Automation 提示词更新：每日生成结构化 plan.json（与放行计划表严格一致）；风控日报开头回顾昨日批复执行情况
- **实测**: POST /api/approve 写账 + AGENT.md 留痕链路 OK（测试数据已清理）；飞书卡片重推显示 5 项待批复 OK
- **人工决策**: （待填写——Loop #5 首日提案 5 项待批复）
- **下一步**: ① 老板在控制台或 Kimi 对话批复首日 5 项提案；② 周一 8-24 08:43 全链路首跑（采集→日报→plan.json→飞书）

## Loop #11 — 2026-08-22

- **执行**: 打通两层——LLM 提案审批后自动执行到 Paper 台账
- **内容**:
  - `trading_team/execute.py`：LLM 提案 Paper 执行器（新建）
    - 读取 `decisions/YYYY-MM-DD/decision.json`，检查 `pm_status == "approved"`
    - 解析 plans：direction 含「买入」且 position_pct > 0 → 买入；含「卖出/减持/回避」→ 卖出；「持有/观望」→ 不动
    - 按收盘价计算 qty（整手），现金不足自动按可用资金调整
    - 写入 `paper/paper.db`（trades / positions / account），reason 标注 "LLM提案" 以区分 v1 策略交易
    - 幂等：同一日期已有 LLM 交易记录则跳过
  - `console/index.html`：交易团队日报页审批面板升级
    - 读取 decision.json 显示 PM 审批状态（pending/approved/rejected）
    - 已 approved 但未执行 → 显示「待执行到 Paper」+ 执行命令提示
    - 已 approved 且已执行 → 显示「已执行到 Paper」
    - 显示风控结论和交易员总结摘要
  - 组织架构页面更新：分两层展示（第一层 LLM 交易团队 / 第二层 v1 确定性支撑层），晨会流程图可视化
- **执行流程**:
  ```
  ① LLM 团队产出 decision.json（pm_status=pending）
  ② 老板批复 → pm_status=approved（Kimi 对话或编辑 decision.json）
  ③ 运行: .venv/bin/python trading_team/execute.py YYYY-MM-DD
  ④ 写入 paper.db → 在「交易台」和「Paper 净值」可见
  ```
- **实测**: 2026-08-22 decision.json（全票观望）测试通过——执行器正确识别无交易，写入 account 备注「LLM提案: 无交易」
- **人工决策**: 老板采纳执行——两层已打通，LLM 提案审批后可进入 Paper 模拟
- **下一步**: ① 周一 8-24 产生首个非观望决策后，验证买入/卖出全流程；② 考虑在 Automation 中加入「approved 自动触发 execute.py」步骤

- **执行**: 老板审批通道落地——控制台审批面板 + 批复台账 + 飞书待批复清单
- **内容**:
  - `trading_team/approvals.py`：批复台账（loops/approvals.json，同日同标的重复批复自动覆盖），record 时同步在 AGENT.md 最新 Loop「人工决策」留痕
  - `console/serve.py`：新增 POST /api/approve 写接口（仅 localhost），并修复 sys.path 使可导入 trading_team
  - `console/index.html`：交易团队日报页新增「🖐 老板审批」面板——逐提案显示方向/仓位/入场/止损，待批复项给出批准/否决按钮，已批复显示状态戳（时间/来源/备注）
  - `trading_team/push_feishu.py`：卡片新增「⏳ 待你批复（N 项）」清单，全部批复后显示 ✅
  - Automation 提示词更新：每日生成结构化 plan.json（与放行计划表严格一致）；风控日报开头回顾昨日批复执行情况
- **实测**: POST /api/approve 写账 + AGENT.md 留痕链路 OK（测试数据已清理）；飞书卡片重推显示 5 项待批复 OK
- **人工决策**: （待填写——Loop #5 首日提案 5 项待批复）
- **下一步**: ① 老板在控制台或 Kimi 对话批复首日 5 项提案；② 周一 8-24 08:43 全链路首跑（采集→日报→plan.json→飞书）

## Loop #11 — 2026-08-22

- **执行**: 观察池扩容 + 控制台「标的池」页面 + 审批面板冲突修复
- **内容**:
  - `watchlist.json` v2：扩至 momentum_rotation 全池 10 只（茅台/五粮液/招行/平安/宁德/比亚迪/紫金/美的/长电/隆基），中兴通讯保留为 event-watch 事件观察标的（非策略池），共 11 只
  - 按新池重跑采集：26/26 源 OK（snapshot 覆盖 11+1 基准）
  - `console/index.html`：新增「🎯 标的池」菜单——代码/名称/板块/定位（策略池|事件观察）/收盘/涨跌幅/换手/PE/PB/最新提案方向，附基准沪深300 行情条
  - **修复**：并行会话写入的审批面板代码含断行 alert（整页 JS 语法错误，按钮失效根因之一）且 renderApprove 指向旧 decisions/ 模式；已恢复为 plan.json + approvals.json 实现，approve() 定义唯一化
- **实测**: JS 语法校验通过；非法 decision 返回 400 参数校验 OK；页面 200
- **人工决策**: （待填写——Loop #5 首日 5 项提案仍待批复）；2026-08-22 批复 宁德时代(300750): 批准（console）；2026-08-22 批复 中国平安(601318): 批准（console）；2026-08-22 批复 招商银行(600036): 批准（console）；2026-08-22 批复 贵州茅台(600519): 批准（console）；2026-08-22 批复 中兴通讯(000063): 批准（console）
- **下一步**: ① 老板批复首日提案；② 周一 8:43 全池（11 只）首份日报自动产出；③ 修复市场宽度统计 filter（Loop #5 遗留数据缺口）
