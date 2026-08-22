# PRD — 个人 A 股量化闭环环境

> v1.0 · 2026-08-22 · 对应 GOAL.md · 参考 [TradingAgents](https://quant-wiki.com/ai/aiquant/TradingAgents/)

## 1. 背景

已有基础：RQAlpha v6.3.0 回测环境、全 A 股日线数据包、动量轮动策略（基线年化 10.4% / 夏普 0.458）、
KDE 支撑位算法完整验证链。缺的是**把这些资产组织成闭环**：每次迭代有固定流程、有状态记录、有复盘产出。

## 2. 角色映射（模拟投资公司）

| 公司角色 | 本系统实现 | 阶段 |
|---------|-----------|------|
| 数据部门 | RQAlpha bundle 更新 + 新鲜度检查 | `data` |
| 技术分析师 | 策略注册表 + 信号计算 | `strategy` |
| 风控委员会 | 回测门禁（阈值判定） | `backtest` |
| 交易员 | Paper 账户执行（SQLite 台账） | `paper` |
| 投委会 | 复盘报告 + AGENT.md 迭代日志 | `review` |
| 多空研究员 / LLM 辩论 | **Phase 3**（LLM 智能体） | — |

## 3. 功能需求

### F1 数据阶段 `data`
- 检查本地 bundle 覆盖范围与最新交易日
- 每月 1 号后可触发 `rqalpha download-bundle` 更新
- 产出：数据覆盖统计（股票数、日期范围、距今天数）

### F2 策略阶段 `strategy`
- 扫描 `strategies/`，维护策略注册表（名称、参数、状态）
- 策略状态机：`research → candidate → live → retired`
- 只有 `live` 状态的策略参与 paper

### F3 回测阶段 `backtest`
- 对 `candidate/live` 策略执行 RQAlpha 回测（统一区间、统一基准沪深300）
- 门禁阈值（v1）：年化收益 > 5% 且 最大回撤 < 30% 且 夏普 > 0.3
- 产出：指标表 + 通过/拒绝结论

### F4 Paper 阶段 `paper`
- 读取 `live` 策略，用最新数据计算目标持仓
- 以最新收盘价模拟成交，更新 SQLite 台账（cash / positions / equity / trades）
- 产出：当日操作清单、账户快照、累计净值

### F5 复盘阶段 `review`
- 汇总本次 loop 各阶段产出，生成 `reviews/YYYYMMDD.md`
- 向 `AGENT.md` 追加迭代日志（目标、变更、结果、下一步）
- 产出：复盘报告 + AGENT.md 新条目

### F6 控制台 `console`
- 本地 Web 页面，读取 `status.json` 单一事实源
- 展示：五阶段状态灯、最近一次执行结果、paper 净值、迭代时间线
- 只读，不操作（操作走 CLI，避免误触）

### F7 编排 `pipeline/run.py`
- `python -m pipeline.run` 跑全流程；`--stages data,paper` 跑子集
- 每个阶段写 `status.json`（状态/耗时/产出摘要/错误）

## 4. 非功能需求

- 全程离线可跑（除数据更新需网络）
- 每阶段独立可重跑、失败不污染其他阶段
- 所有产出落盘可审计（文件/SQLite/MD）
- 敏感凭证不入库（.gitignore 强制）

## 5. 里程碑

| 阶段 | 内容 | 状态 |
|------|------|------|
| P1 | 五阶段闭环 + 控制台 + AGENT.md 协议 | 🚧 本次迭代 |
| P2 | 策略库扩充（均值回归、ETF 轮动）、门禁参数化、定时调度 | 待启动 |
| P3 | LLM 多空研究辩论（TradingAgents 完整版）、飞书日报推送 | 待启动 |
| P4 | 实盘评审（另立项目，需人工批准） | 未批准 |

## 6. 风险

- 数据包月度更新 → paper 使用的是上一交易日收盘，T+1 语义需在复盘注明
- 单策略过拟合 → 门禁 + 复盘强制样本外讨论
- 本地环境单点 → 代码全量 Git 管理，结果可重算
