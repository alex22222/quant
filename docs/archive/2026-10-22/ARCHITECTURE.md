# ARCHITECTURE — 闭环流水线架构

> v1.0 · 2026-08-22

> 当前演进目标与运行契约见 `docs/AUDITABLE_A_SHARE_SYSTEM.md`。旧版 `status.json`
> 继续服务策略流水线概览；研究与 Paper 工作流的运行事实源已迁移为
> `trading_team/.runs/ledger.db`，不能再用“输出文件存在”代替运行成功。

## 总览

```
┌─────────┐   ┌───────────┐   ┌──────────┐   ┌─────────┐   ┌──────────┐
│  data   │ → │ strategy  │ → │ backtest │ → │  paper  │ → │  review  │
│ 数据部门 │   │ 技术分析师 │   │ 风控门禁  │   │ 交易员  │   │ 投委会   │
└────┬────┘   └─────┬─────┘   └────┬─────┘   └────┬────┘   └────┬─────┘
     │              │              │              │             │
     └──────────────┴──────┬───────┴──────────────┴─────────────┘
                           ▼
                分域事实源（ledger / manifest / approval / Paper DB）
                           │
                     ┌─────▼─────┐         ┌──────────┐
                     │  console  │         │ AGENT.md │ ← loop 日志
                     │ (Web 看板) │         └──────────┘
                     └───────────┘
                           │
              paper/*.db (策略与双 Paper SQLite 台账)
```

## 目录约定

```
pipeline/     五阶段代码 + 编排器
strategies/   策略文件（注册表来源）
paper/        paper.db + llm_gate.db + llm_no_gate.db（SQLite 台账）
reviews/      每次 loop 的复盘报告
console/      Web 控制台（静态页 + serve.py）
status.json   旧策略流水线概览（不是研究/Paper 唯一事实源）
AGENT.md      Loop Engineering 协议 + 迭代日志
```

## 关键设计决策

1. **分域事实源**：策略流水线概览继续写 `status.json`；研究工作流状态写
   `trading_team/.runs/ledger.db`；数据质量写日期 manifest；审批绑定 canonical
   `plan_hash`；委托、成交和账户状态只认 Paper SQLite。控制台按领域读取这些事实源，
   不从日志或 Markdown 反推状态。
2. **paper 与回测解耦**：paper 阶段不复用 RQAlpha 引擎，而是直接用
   `kde_levels/levels.py` 的 BundleData 读最新数据、独立计算信号、以收盘价模拟成交。
   原因：paper 的语义是"每日增量执行"，回测引擎是"全区间重放"，混用会导致状态错乱。
3. **门禁在 backtest 阶段**：策略通过阈值才能 live；paper 只信任注册表里的 live 状态，
   不自己判断。
4. **AGENT.md 由 review 阶段追加**：人可编辑，机器只追加，冲突时以人编辑为准。

## 数据流

| 阶段 | 输入 | 输出 |
|------|------|------|
| data | ~/.rqalpha/bundle | status.data（覆盖范围、新鲜度） |
| strategy | strategies/*.py + status.strategies | 注册表（含状态机） |
| backtest | candidate/live 策略 | reports + 门禁结论 + status.backtest |
| paper | live 策略 + bundle 最新数据 + paper.db | 成交记录、账户快照、净值 |
| review | 以上全部 | reviews/*.md + AGENT.md 条目 |

研究与双 Paper 链路采用另一条版本化数据流：

`data_collect -> LLM roles -> plan.json -> decision.json(plan_hash) -> daily_finalize -> dual Paper/counterfactual`

`data_collect` 将快照、逐标的技术/基本面、新闻、会话一致性和情绪采集记录为独立步骤。
重跑只复用输入和输出哈希均匹配的成功步骤；观察池覆盖不完整、技术日期不一致或当日尚未
完成收盘时，manifest 失败关闭，后续 LLM 角色不得运行。

双 Paper 数据库共享 `paper_account.v1`：`pending_orders/trades/positions` 保留当前执行事实，
`order_events` 追加最终订单事件，`account_valuations` 按可靠收盘日保存现金、持仓快照、市值、
权益和缺失行情。旧订单只迁入一个标明 `legacy_import` 的最终状态事件，不伪造历史状态转换；
净值历史从 schema 上线日开始积累。

## 技术栈

- Python 3.12 venv（已有）：rqalpha / pandas / scipy / h5py / ccxt
- SQLite（标准库）：paper 台账
- 控制台：零依赖静态 HTML + `python3 serve.py`（npm run dev 转发端口）
