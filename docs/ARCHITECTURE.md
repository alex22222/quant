# ARCHITECTURE — 闭环流水线架构

> v1.0 · 2026-08-22

## 总览

```
┌─────────┐   ┌───────────┐   ┌──────────┐   ┌─────────┐   ┌──────────┐
│  data   │ → │ strategy  │ → │ backtest │ → │  paper  │ → │  review  │
│ 数据部门 │   │ 技术分析师 │   │ 风控门禁  │   │ 交易员  │   │ 投委会   │
└────┬────┘   └─────┬─────┘   └────┬─────┘   └────┬────┘   └────┬─────┘
     │              │              │              │             │
     └──────────────┴──────┬───────┴──────────────┴─────────────┘
                           ▼
                     status.json  ←── 单一事实源
                           │
                     ┌─────▼─────┐         ┌──────────┐
                     │  console  │         │ AGENT.md │ ← loop 日志
                     │ (Web 看板) │         └──────────┘
                     └───────────┘
                           │
                     paper/paper.db (SQLite 台账)
```

## 目录约定

```
pipeline/     五阶段代码 + 编排器
strategies/   策略文件（注册表来源）
paper/        paper.db（SQLite 台账）
reviews/      每次 loop 的复盘报告
console/      Web 控制台（静态页 + serve.py）
status.json   各阶段最新状态（唯一事实源）
AGENT.md      Loop Engineering 协议 + 迭代日志
```

## 关键设计决策

1. **status.json 单一事实源**：所有阶段只写 status.json 自己的段落；控制台只读它。
   阶段间不直接调用，通过文件系统产物解耦（可单独重跑任何阶段）。
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

## 技术栈

- Python 3.12 venv（已有）：rqalpha / pandas / scipy / h5py / ccxt
- SQLite（标准库）：paper 台账
- 控制台：零依赖静态 HTML + `python3 serve.py`（npm run dev 转发端口）
