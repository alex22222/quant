# A 股 AI 量化系统改造与策略库建设方案

> 日期：2026-08-25  
> 范围：`quant-wiki` 三个论文索引页、相关原始论文、当前 `quant` 仓库  
> 定位：Research / Paper only；本方案不授权实盘交易  
> 方法：Loop Engineering——发现 → 假设 → 实验 → 验证 → 记录 → 晋级/淘汰

## 0. 执行结论

### 总判断

当前项目已经具备比“再加一个 AI 模型”更重要的基础：确定性流水线、统一策略信号接口、A 股 T+1 Paper 撮合、策略状态机、晋级证据哈希、实验台账、DSR、多阶段回测、PIT 股票池、LLM 提案的确定性风控与人工审批。

但它还不是一个可信的 A 股策略工厂，原因不是策略数量少，而是以下五个断点仍未闭合：

1. 数据只有交易日口径，没有完整的“市场当时何时可见”口径；财报、公告、指数成分、风险警示、交易规则等还没有统一的双时点版本。
2. 论文、研究假设、因子公式、参数试验、回测结果之间缺少可审计的一对一血缘。
3. 预测、选股、组合、订单、成交是混合评估的；模型预测准确不等于可投资收益。
4. 成本模型仍以固定滑点和“开盘触及涨跌停即不可成交”近似，不能表达集合竞价、封单、容量、部分成交和规则历史变化。
5. LLM 团队主要被评价为报告质量或 T+5 方向准确率，尚未经过 point-in-time、成本后组合收益和纯前向复现验证。

因此本方案的直接结论是：

- **实盘：No-Go。** 当前 `status.json` 为 0 个 live 策略，这一状态应保持。
- **系统优先级：数据时点与研究治理 > 组合构建 > 新模型 > 新 Agent。**
- **策略库优先级：先形成 4 个经济逻辑独立、可被证伪的策略族；不批量生成几十个相似策略。**
- **LLM 的正确位置：研究副驾驶，不是收益裁判或订单生成器。**

### 目标架构

```text
论文/研报/公告/行情
        ↓
Evidence Registry（来源、版本、主张状态、复现状态）
        ↓
Bitemporal Data（event_time / available_time / ingest_time）
        ↓
Feature & Event Store（公式、血缘、滞后、覆盖率）
        ↓
Hypothesis Registry（先登记假设，再看结果）
        ↓
Research Harness（purged walk-forward / FDR / DSR / PBO）
        ↓
Portfolio Lab（行业/风格/风险/换手/容量约束）
        ↓
A-share Execution Twin（T+1 / 板块规则 / ST / IPO / 集合竞价 / 涨跌停队列）
        ↓
Shadow / Paper Forward（实现差额、拒单、漂移、失效监控）
        ↓
人工晋级；实盘仍需另行明确授权
```

---

## 1. 论文阅读范围与证据分级

### 1.1 页面覆盖

本轮扫描了三个页面的全部目录：

- [2025 年 AI 量化论文优选 41 篇](https://quant-wiki.com/ai/2025-ai-paper/)；页面正文标题实际写的是“2024 年 AI 量化论文优选 41 篇”，大部分条目发表于 2024 年。
- [2024 年 AI 量化论文精选 48 篇](https://quant-wiki.com/ai/2024-ai-paper/)。
- [2024 年 LLM 量化论文](https://quant-wiki.com/ai/2024-llm-quant-paper/)；该页是按预测、情感、时序、模型开发和基准分类的综述，不是逐篇完整论文评述。

没有把所有条目等权对待。期权定价、DeFi、外汇、信用、能源市场和纯宏观理论论文被登记为低相关；对当前 A 股股票策略、投研系统和组合构建有直接价值的论文追到了原始摘要或全文页面。

### 1.2 证据等级

| 等级 | 定义 | 系统动作 |
|---|---|---|
| E0 | 索引页一句话或营销性描述 | 只能进入线索池 |
| E1 | 原始论文方法与实验已读，但未复现 | 可写研究卡，不可称为本项目 alpha |
| E2 | 使用本项目 PIT A 股数据复现，代码/数据/结果可重跑 | 可进入 research |
| E3 | 冻结假设后通过 purged walk-forward、成本、FDR/DSR、容量检验 | 可进入 candidate |
| E4 | 纯前向 Shadow/Paper 达到足够事件数且实现差额可解释 | 可申请 Paper 代表策略 |
| E5 | 合规、风控、人工审批均通过 | 才能讨论小资金实盘；不在本方案授权范围内 |

任何 E0/E1 的论文自报 Sharpe、收益率、ACC 或专家评分，都不得抄入本项目绩效看板。

### 1.3 对当前项目最有价值的论文结论

| 论文/方向 | 原始证据告诉我们什么 | 不能直接推导什么 | 本项目决策 |
|---|---|---|---|
| [CausalStock](https://arxiv.org/abs/2411.06391) | 用滞后因果图结合 LLM 新闻去噪；在包含中国市场的数据集上比较 ACC/MCC，并做每日 Top-3 等权模拟 | ACC/MCC 和 Top-3 模拟不能证明在 A 股 T+1、涨跌停、费用与容量下可交易 | 复用“关系图 + 新闻去噪”，不复用其收益结论 |
| [FinRobot Equity Research](https://arxiv.org/abs/2411.08804) | Data-CoT、Concept-CoT、Thesis-CoT 将数据、概念和投资论点分层 | 专家对准确性/逻辑性/叙事性的打分不是 alpha 或风控验证 | 用作研究报告工作流，不作为策略模型 |
| [GPT-Signal](https://arxiv.org/abs/2410.18448) | LLM 可基于已有因子提出公式化新特征，再用相关性/Fama-MacBeth 检验 | 6 个新信号、有限行业与相关性改进不足以证明成本后 OOS 收益 | 建设 Alpha DSL + 假设注册；生成后必须独立检验 |
| [QuantAgent](https://arxiv.org/abs/2402.03755) | 内循环检索/改进，外循环用环境结果更新知识库 | 自动把回测好结果写回知识库会放大 HARKing 和验证集污染 | 只回写“可复现事实与失败原因”，不自动晋级参数 |
| [Alpha-GPT 2.0](https://arxiv.org/abs/2402.09746) | Human-in-the-Loop 覆盖 alpha mining、modeling、analysis | “有人参与”本身不构成统计有效性 | 人负责冻结假设与审批，机器负责执行和审计 |
| [M6 Forecasting Competition](https://arxiv.org/abs/2310.13357) | 预测排名与投资表现联系很弱；风险模型、组合预测和群体信息更有价值 | 更高分类准确率不保证更高组合收益 | 把预测、组合、执行分开评分；先建 Portfolio Lab |
| [Extracting Alpha from Financial Analyst Networks](https://arxiv.org/abs/2410.20597) | 分析师共同覆盖网络可表达公司之间的信息溢出 | 海外分析师覆盖数据和论文自报 Sharpe 不能迁移到 A 股 | 用 A 股分析师覆盖/供应链/行业关系做研究图谱 |
| [Cluster-driven Hierarchical Representation](https://doi.org/10.1145/3677052.3698676) | 大股票池可用 signed graph clustering 做分层组合，降低直接估计大协方差矩阵的脆弱性 | 不能在没有稳定预期收益和成本估计时直接替代基准组合 | 作为组合 challenger；baseline 先用简单约束打分 |
| FININ 新闻交互模型（[索引解读](https://quant-wiki.com/ai/2024-ai-paper/)） | 新闻存在延迟、长记忆和相互作用，单一情感分数不够 | 海外新闻语料和论文 Sharpe 不能直接迁移 | 事件链、来源、发布时间、关联公司与衰减必须入库 |
| Regime Switching Dynamic Factor Allocation（同上） | 因子权重可随市场状态变化 | 不能用全样本最优状态阈值回看式切换 | 先冻结状态定义，再用 ETF/因子组合前向验证 |
| Realized Volatility Spillover GNN（同上） | 一阶邻居的波动溢出可能比多跳更稳定 | 日线 OHLC 无法复现已实现波动率网络 | 只有获得可靠高频数据后才研究；先做日频风险图 |
| Online Conformal Multi-step Forecasting（同上） | 预测必须给区间并校准覆盖率，不应只给点预测 | 区间覆盖率好不等于可交易 | 将区间和校准误差加入风险预算，不把它当 alpha |
| Factor Momentum vs Price Momentum（[索引解读](https://quant-wiki.com/ai/2025-ai-paper/)） | 因子动量与个股/行业价格动量不是同一个异常 | 在固定十只股票上切换动量参数无法检验此结论 | 策略库分开管理“个股截面动量”和“因子收益动量” |
| Imb-FinDiff（同上） | 扩散模型可改善表格少数类合成 | 合成涨跌停/暴跌样本不能作为真实尾部收益证据 | 仅用于分类器压力测试，禁止进入 PnL 评估样本 |
| Efficient Trading with Price Impact / Liquidity Fragility（同上） | 流动性和冲击成本具有状态依赖与重尾 | 固定 0.2% 滑点不能证明容量 | 建立可成交容量与压力滑点曲线 |

### 1.4 一个必须保留的反例

`2024-llm-quant-paper` 将“FinancialStatementAnalysis”描述为 LLM 预测盈余变化优于人类分析师。但作者 Valeri Nikolaev 的[当前项目页](https://faculty.chicagobooth.edu/valeri-nikolaev/ongoing-research-projects)明确写明：合作者在复现时发现底层数据和分析不一致，作者本人确认后已暂时撤回该工作论文并复查其他相关论文。

这不是否定 LLM 财报分析，而是确定一条系统纪律：

> 论文的“发表/预印本存在”与“结论可复现”必须是两个字段。任何论文状态变化，都应自动令派生策略证据失效并退回 research。

---

## 2. 当前项目基线

### 2.1 已有且应保留

| 能力 | 现有落点 | 评价 |
|---|---|---|
| 五阶段闭环 | `pipeline/run.py`、`docs/ARCHITECTURE.md` | 可作为总编排骨架 |
| 统一信号接口 | `strategies/base.py` | 保留；扩展为带权重/置信区间/血缘的输出 |
| PIT 股票池 | `pipeline/universe.py`、月度快照 | 正确方向；需加入历史指数成分与规则元数据 |
| 门禁与多重试验 | `pipeline/gate.py`、`experiment_ledger.jsonl` | 已有 DSR；需补 PBO/FDR、冻结数据切片和家族级试验数 |
| 原子晋级证据 | `pipeline/promote.py`、`stage_audit.py` | 保留；增加论文/数据/特征/模型哈希 |
| A 股 Paper 撮合 | `paper/execution_model.py` | 已覆盖基础 T+1/费用/涨跌停；仍是简化模型 |
| LLM 三层契约 | `trading_team` prompts、schemas、consistency check | 保留；研究评分与交易评分分离 |
| 研究摄取 | `pipeline/research_ingest.py` | 目前是关键词→固定模板，不能称为论文复现 |

### 2.2 当前实证状态

- `status.json` 记录 0 个 live 策略；既有候选在最近门禁结果中均为 reject。
- 现有测试的正确执行范围不是仓库根目录无参数 `pytest`；后者会误收集内嵌 `rqalpha/` 的上游测试。
- 本轮对 `tests execution/tests` 的基线是 **170 passed / 6 failed**。6 个失败均因工作区正在删除旧策略文件，但 `tests/test_turtle_core.py` 仍硬编码读取这些文件。
- 这说明策略文件删除、策略注册表、门禁报告、测试参数表和历史墓碑尚未原子同步。

### 2.3 A 股规则适配的现存缺口

当前 `paper/execution_model.py` 通过代码前缀推断 10%/20% 涨跌幅，买入统一按 100 股整手，并在开盘恰好涨跌停时直接拒单。至少有以下缺口：

1. 科创板现行规则的限价/市价申报最低数量为 200 股；不能用全市场统一 100 股。见[上交所 2026 年交易规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)。
2. 科创板和创业板 IPO 前 5 个交易日不设涨跌幅限制；现有按代码前缀常数会误判。见[深交所创业板说明](https://investor.szse.cn/knowledge/stock/chinext/t20200729_580056.html)及上交所规则。
3. ST/风险警示、退市整理、临停、除权除息、规则变更生效日没有进入证券级规则表。
4. 涨跌停价不代表绝对零成交；是否成交取决于申报价、队列、封单量和可用成交量。日线模型只能做保守上下界，不能伪造精确成交。
5. 证券程序化交易报告和高频交易监管细则已自 2025-07-07 实施；若未来进入自动实盘，必须在 broker adapter 之前增加合规状态门。见[上交所实施细则答记者问](https://star.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20250403_10776805.shtml)。

---

## 3. 系统改造：八个建设包

## WP0：先修研究事实源和 CI 边界

### 建设内容

1. 增加根目录 `pytest.ini` 或 `pyproject.toml`，固定 `testpaths = tests execution/tests`，上游 `rqalpha/tests` 作为独立可选 job。
2. 策略状态不再通过“文件是否存在”表达历史；建立 `strategy_registry.json` 或数据库表，保留 tombstone：`retired/deleted_at/reason/last_hash`。
3. 删除策略必须经单一命令执行，同一事务更新注册表、测试参数、活动门禁目标和墓碑；历史报告只读保留。
4. `status.json` 继续做运行快照，但长期事实改为 append-only ledger，避免运行状态和历史治理混在一个可编辑 JSON。

### 验收

- `pytest -q` 只运行项目测试且绿灯。
- 删除一个临时策略后，注册表、测试、活动门禁一致；历史报告仍可查。
- 任意未登记的策略文件、孤儿测试或 orphan report 会被 audit 拒绝。

## WP1：双时点 A 股数据底座

### 数据契约

所有可用于特征的记录至少包含：

```text
event_time       经济事实所属时间，如 2026Q2
published_at     交易所/公司正式发布时间
available_at     本系统策略最早允许使用时间
ingested_at      本地获取时间
revision_id      更正/重述版本
source_uri       原始来源
source_hash      原文哈希
```

其中 `available_at` 是回测唯一允许的切点。例如 18:30 公告只能用于下一个交易日，不能因为财报的 `event_time=季末` 而回填到季末。

### 数据域

- `security_master_pit`：板块、上市/退市、ST、停牌、涨跌幅规则、申报单位、两融资格。
- `universe_membership_pit`：沪深300/中证500/中证800历史成分及调入调出公告时间。
- `market_daily`：不复权价、复权因子、成交量额、涨跌停价、停牌原因。
- `fundamental_pit`：原始财报、快报、业绩预告、更正公告、TTM 派生值。
- `event_pit`：回购、增减持、解禁、分红、重大合同、监管问询、并购、停复牌。
- `text_corpus`：公告、问询回复、财报、新闻；保留正文、段落、实体与引用位置。
- `relation_graph_pit`：行业、供应链、共同分析师覆盖、共同股东、指数共现；边也必须有生效时间。

### 验收

- 随机抽 100 个“公告后下一交易日”案例，回测在公告前不可见、公告后才可见。
- 复权前后总收益、分红、拆并股可对账。
- 每次研究都能生成 dataset manifest 与内容哈希。
- PIT 覆盖率、迟到率、修订率每天可见；关键域覆盖不足时 fail closed。

## WP2：Evidence Registry + Hypothesis Registry

### Paper Card

每篇进入研究的论文保存：

```yaml
paper_id: arxiv:2411.06391
claim: news-denoised temporal causal graph predicts multi-stock direction
market: [US, CN, JP, UK]
target: next-period direction
metrics: [ACC, MCC, APV, Sharpe]
code_available: true/false
paper_status: active/withdrawn/corrected
replication_status: not_started/failed/partial/passed
a_share_transfer_risks: [T+1, limit, point_in_time_news, cost, capacity]
```

### Hypothesis Card

在任何回测前登记：

```yaml
hypothesis_id: H-2026-001
economic_mechanism: 供应链上游重大订单公告对下游存在1-3日信息扩散
universe: PIT CSI800 ex-ST, listed>=250 sessions
signal_formula: ...
availability_lag: next_open
holding_period: 5 sessions
expected_sign: positive
primary_metric: net long-only factor spread after costs
family: event_relation_spillover
falsification: OOS spread<=0 or sign flips in >1/3 folds
frozen_at: ...
```

LLM 可以草拟卡片，但只有人确认并冻结后，实验器才接受。

### 验收

- 无 Paper Card 的外部主张不得进入策略代码。
- 无冻结 Hypothesis Card 的回测只标记 `exploratory`，永远不能晋级。
- 论文被撤回或数据版本变化时，依赖图上的策略证据自动失效。

## WP3：Feature Store 与 Alpha DSL

### 为什么需要

当前 `research_ingest.py` 是关键词命中后选择 momentum/breakout/MA/reversal 固定模板，容易把不同论文翻译成同一个动量策略。GPT-Signal 的真正启发不是“让 GPT 随便写 Python”，而是让 LLM 在受限公式语言中提出可审计特征，再由确定性引擎计算和检查。

### DSL 最小集合

- 横截面：`rank/zscore/winsorize/neutralize/group_rank`
- 时序：`delay/delta/ts_mean/ts_std/ts_rank/decay`
- 财务：`ttm/yoy/qoq/accrual/cash_conversion`
- 事件：`event_count/event_surprise/time_since/graph_neighbor_agg`
- 风险：`beta/residual_vol/downside_vol/turnover/liquidity`

每个 Feature 必须保存：公式 AST、输入字段、最大 lookback、最小 lag、空值策略、行业中性方式、生成者、人类审阅者、代码哈希和测试。

### 静态检查

- `delay >= required_availability_lag`
- 禁止未来窗口和负 shift
- 截面算子不得跨交易日混用
- 财务字段必须走 `available_at`
- 合成数据不得出现在收益标签或回测行情中
- 同族因子计算相关性和表达式相似度，防止同义复制

### 验收

- 同一 manifest + DSL AST 得到完全一致的特征值。
- 因子值随机抽样可追溯到原始记录和公告段落。
- 新因子自动产出覆盖率、稳定性、截面分布、行业暴露和与在库因子相关矩阵。

## WP4：研究与复现引擎 v3

### 实验切分

不再用一次固定 70/30 或已经被看过的 2024-2026 窗口充当 OOS。采用：

1. Train：拟合、特征选择。
2. Validation：参数选择；使用后即永久标记 touched。
3. Purged Walk-forward：按持有期 purge，相邻窗口加 embargo。
4. Locked Test：一次性打开；打开后策略版本冻结。
5. Forward Shadow：真实时间顺序积累，禁止回填修改预测。

### 统计门禁

- 家族级实验次数进入 ledger，不按文件名分散计数。
- DSR 保留；新增 FDR（对同期探索的因子族校正）和 PBO/CSCV。
- 报告 IC/RankIC、单调性、衰减、分组收益、行业/风格暴露，不只报告组合净值。
- 对选择偏差做 bootstrap；不把相关性 `|r|=0.1` 写成强 alpha。
- 模型类任务同时报告 calibration、Brier/LogLoss、区间覆盖率和经济效用。

### 经济门禁

统一分解：

```text
gross alpha
- next-open gap
- commission/tax/transfer fee
- spread/slippage
- limit/suspension non-fill loss
- market impact/capacity loss
= executable net alpha
```

### 验收

- 所有结果可由单命令从 manifest 重跑。
- 每个被拒实验明确记录是哪一道门失败。
- 预测指标和投资指标分屏展示，禁止用 ACC 代替净收益。

## WP5：Portfolio Lab——把预测变成可投资组合

M6 的核心教训是“预测好”和“投得好”之间没有天然映射。因此策略库必须把 Signal 和 Portfolio Policy 分开。

### Baseline

先实现简单、可解释的长仓组合：

- 截面分数 top quantile；
- 行业相对中性；
- 单票、行业、风格暴露上限；
- 波动率缩放；
- 最低成交额和持仓容量；
- turnover penalty 与 no-trade band；
- 现金缓冲；
- 不依赖融券。

### Challenger

- hierarchical/signed-graph clustering；
- shrinkage covariance；
- regime-conditioned factor weight；
- conformal uncertainty 决定仓位折扣；
- 多信号 ensemble，但只组合低相关、独立来源的策略族。

### 组合验收

- 与等权、沪深300/中证800、简单多因子 baseline 比较。
- 报告 signal return、selection return、allocation return、timing return、cost drag。
- 优化器必须有 infeasible fallback；失败时退到现金/基准，不允许输出异常权重。

## WP6：A 股 Execution Twin v2

### Rule Registry

不再用代码前缀硬编码：

```yaml
symbol: 688xxx.XSHG
effective_from: <official_rule_effective_date>
board: STAR
buy_lot_min: 200
limit_pct: 0.20
ipo_no_limit_days: 5
st_rule: ...
settlement: T+1
auction_rules: ...
```

规则记录必须按生效日版本化，并从官方交易所规则生成测试案例。

### Fill Model 层级

- L0：日线保守上下界，用于广泛筛选。
- L1：分钟线 + 集合竞价成交量，估计 next-open/VWAP 与部分成交。
- L2：盘口/逐笔数据，才允许研究涨跌停队列、LOB 或高频执行。

没有 L2 时，LOB-LLM、做市和高频论文一律标记 `data_blocked`，不得用日线伪复现。

### 容量与压力测试

- 下单金额 / 20 日 ADV；
- 集合竞价参与率；
- 价差与波动状态；
- 连续跌停无法卖出；
- 停牌和重大事件流动性冻结；
- 冲击成本基准、悲观、极端三条曲线。

### 验收

- 官方规则 fixture 覆盖主板、创业板、科创板、ST、IPO 前五日和退市整理。
- 同一 Target 在 backtest/Paper 得到同样订单与拒绝原因。
- 每笔未成交有队列/流动性或规则原因，不再只有布尔拒单。

## WP7：LLM Research Plane 与确定性 Trading Plane

### Research Plane

LLM 允许：

- 带引用地抽取公告事实；
- 将论文方法翻译为 Hypothesis Card 草案；
- 在 Alpha DSL 中提出特征；
- 解释失败实验和生成复盘；
- 对报告做一致性检查。

每次调用记录 model/provider/version、prompt hash、context manifest、temperature、输出 schema 和引用。

### Trading Plane

仅确定性代码允许：

- 计算特征和标签；
- 生成权重；
- 检查规则、风险和审批；
- 写 Paper ledger；
- 计算绩效与晋级门禁。

LLM 的 bullish/bearish/confidence 不直接进入订单。若未来要把 LLM 输出当因子，必须先固化为带时间戳的数值特征，并与其他因子一样走 E2-E4。

### 评价体系

| 层 | 指标 |
|---|---|
| 抽取 | exact match、字段覆盖、引用正确率、时间戳正确率 |
| 研究报告 | 事实准确、证据完整、矛盾率、稳定性 |
| 预测 | calibration、Brier/LogLoss、IC、衰减 |
| 组合 | 成本后超额、IR、回撤、尾损、容量 |
| 执行 | fill ratio、implementation shortfall、拒单率、规则一致性 |

T+5 方向准确率只能留在预测层，不能作为策略晋级主指标。

---

## 4. A 股策略库的形成方案

## 4.1 策略不是 Python 文件，而是版本化研究对象

建议 Strategy Card schema：

```yaml
strategy_id: AS-CROSS-001
version: 1.0.0
family: cross_sectional_multifactor
hypothesis_ids: [H-2026-001, H-2026-002]
feature_versions: [...]
universe_policy: csi800_pit_ex_st
signal_policy: monthly_top_quantile
portfolio_policy: sector_neutral_vol_scaled
execution_policy: next_open_l1
risk_policy: long_only_no_margin
data_manifest: sha256:...
code_hash: ...
paper_evidence: [...]
state: specified
promotion_evidence: null
```

状态机建议：

```text
idea
→ specified（假设冻结）
→ replicated（论文/基准复现）
→ research（A股实验）
→ candidate（统计+经济门禁通过）
→ shadow（不可交易、实时记录）
→ paper（允许模拟撮合）
→ live_eligible（仅资格，不自动启用）
→ live（必须另行人工授权）
→ retired
```

任何代码、参数、数据、论文状态、规则版本变化都生成新版本，不修改旧证据。

## 4.2 首批四个策略族

### S1：PIT 宽基准多因子（最高优先级）

**经济逻辑**：价值、盈利质量、低风险、价格动量来自不同机制；在 A 股长仓约束下，用行业中性和换手约束获得稳定的截面基准。

**股票池**：历史时点中证800或流动性前 800；排除当时 ST、上市未满 250 交易日、停牌、低成交额及无有效财报者。

**因子块**：

- value：EP/BP/FCF yield；
- quality：ROE、毛利稳定性、经营现金流/利润、应计项；
- momentum：12-1、6-1、短期反转分离；
- risk/liquidity：低波、beta、换手、Amihud；
- revision/event：业绩预告或分析师一致预期变化（有 PIT 数据才启用）。

**组合**：月度；行业内 rank；top 15%-20%；单票上限；no-trade band；风险缩放。

**为什么先做**：它能成为所有 AI/LLM 策略的最低可比 baseline，并直接修复当前固定十只蓝筹和策略同质化问题。

### S2：财报变化与盈利质量

**经济逻辑**：A 股业绩预告、快报、季报存在分阶段信息释放；现金流、应计项、毛利和存货/应收变化可判断盈利质量。

**LLM 角色**：只从公告中抽取“变化原因、一次性项目、管理层口径、风险事项”，保留段落引用；数值全部由确定性解析器计算。

**标签/持有期**：公告后 next-open 起 5/20/60 日分开研究；同一事件不得用公告前价格。

**反例纪律**：前述已撤回财报 LLM 论文只作为方法线索，不能作为绩效先验。

### S3：公告事件 × 关系图谱溢出

**经济逻辑**：订单、供应链、行业政策、监管问询等信息可能沿公司关系网络延迟传播；对应 CausalStock/FININ/分析师网络论文的可迁移部分。

**图谱边**：行业、供应链、共同分析师覆盖、共同重要股东、指数共现。优先使用确定性来源，LLM 只能做带引用的实体消歧。

**模型顺序**：

1. 简单一阶邻居加权事件分数；
2. 带时滞的 sparse VAR/graph regularization；
3. 数据和 baseline 足够后才试 GAT/CausalStock 类模型。

**关键门禁**：只允许训练窗口发现边权；测试窗口不得用未来关系；事件时间、公告时间、可用时间三者分开。

### S4：市场状态下的因子/ETF 配置

**经济逻辑**：价格动量、因子动量、风险状态是不同信号；A 股不能简单复制美股多空因子，但可在宽基准、红利、成长、价值、小盘等可交易 ETF/长仓篮子之间配置。

**状态变量**：宽基准趋势、横截面扩散度、已实现波动、成交拥挤度、风险溢价代理。

**组合约束**：低频、长仓、现金可选；状态定义在实验前冻结；不允许用事后牛熊标签训练。

**用途**：作为组合风险预算层，不一定单独称 alpha。

## 4.3 第二批/延期族

| 方向 | 处理 |
|---|---|
| 分析师覆盖网络 GAT | 数据采购与 PIT 质量确认后再做；先用简单图聚合 baseline |
| 波动率溢出 GNN | 有可靠 5 分钟/已实现波动率数据后研究 |
| LOB-LLM / 高频执行 | L2/逐笔、撮合回放、程序化交易合规全部具备前延期 |
| 融资融券/做空/杠杆 RL | 不适合当前个人长仓系统；融券可得性和规则是硬约束 |
| 扩散合成少数类 | 仅做分类压力测试；不得扩充收益回测样本 |
| 纯多 Agent 投票 | 保留为研究叙事层，不成为独立策略族 |

## 4.4 策略族晋级门禁

### Specified → Research

- 假设、预期符号、股票池、持有期、主要指标和证伪条件已冻结。
- 数据 `available_at` 检查通过，特征静态检查通过。
- 至少一个简单 baseline；复杂模型不能只和弱 baseline 比。

### Research → Candidate

- purged walk-forward 大多数折不反号；不能靠单一年份贡献全部收益。
- 家族级 FDR 后仍有统计证据；DSR/PBO 不显示明显选择偏差。
- 成本后净超额、IR、回撤、尾损、换手和容量达到预先登记标准。
- 对行业、规模、beta、波动、流动性暴露有归因；去除已知暴露后仍有增量。
- 与在库代表策略的收益相关、持仓重叠和边际组合贡献通过门禁。

### Candidate → Shadow/Paper

- 代码、数据、参数、规则全部冻结。
- 纯前向至少覆盖 120 个交易日，并且低频策略至少 12 次独立调仓/事件批次；事件策略要求足够独立事件数，不能只看时间长度。
- implementation shortfall、未成交损失、规则拒单和数据迟到可解释。
- Shadow 不达标只能退回 research，不能在同一 forward 窗口调参后继续称其为 OOS。

### Live

本方案不设置自动 live。即使达到 `live_eligible`，仍需：

- 用户对具体策略、资金、券商账户、最大损失和启停条件作明确授权；
- 程序化交易报告/券商要求确认；
- kill switch、日损限制、仓位对账、订单幂等和灾备演练通过。

---

## 5. 分阶段实施路线

## Phase 0：稳定事实源（3-5 天）

交付：

- 固定 pytest 发现范围；
- 策略 tombstone 与原子删除命令；
- registry/report/test orphan audit；
- 记录当前 170 pass / 6 fail 的基线并在并发删除完成后复测。

退出条件：项目自有测试全绿，0 live，注册表与文件一致。

## Phase 1：PIT 数据与规则注册表（2-3 周）

交付：

- 双时点 schema 与 manifest；
- security/universe/fundamental/event 四个 PIT 域；
- A 股 Rule Registry；
- 100 个公告可见性 golden cases + 交易规则 fixtures。

退出条件：无 PIT 字段的数据不能进入新研究；回测可复现同一数据快照。

## Phase 2：Evidence/Hypothesis/Feature Registry（2 周）

交付：

- Paper Card、Hypothesis Card、Strategy Card；
- Alpha DSL 与静态防未来检查；
- 论文状态依赖图；
- 因子健康报告和相关矩阵。

退出条件：任一实验能从论文/假设追到公式、数据和结果。

## Phase 3：Research Harness + Portfolio Lab（2-3 周）

交付：

- purged walk-forward、embargo、locked test；
- FDR、PBO、现有 DSR 统一到家族级 ledger；
- baseline 组合、风险约束、成本与容量归因；
- 预测/组合/执行三层报表。

退出条件：简单 baseline 可一键运行，预测好但投资差会被明确拒绝。

## Phase 4：首批策略族（3-5 周）

顺序：

1. S1 PIT 宽基准多因子；
2. S4 因子/ETF 状态配置；
3. S2 财报变化与盈利质量；
4. S3 事件关系图谱。

每个族先做线性/规则 baseline；只有 baseline、数据量和增量证据充分，才允许 Transformer/GNN/LLM challenger。

退出条件：至少一个策略达到 candidate，或所有策略被诚实拒绝并留下可复现失败证据。后者同样算本阶段成功。

## Phase 5：纯前向 Shadow/Paper（6-12 个月）

交付：

- 预测锁定日志；
- L1 撮合实现差额；
- 漂移、数据迟到、规则变化、容量监控；
- 月度投委会报告。

退出条件：达到前向事件数门槛并通过预注册标准；否则退回 research。没有自动实盘步骤。

---

## 6. Loop Engineering 运行协议

### 单次研究循环

```text
Trigger: 新论文 / 新公告数据 / 新策略假设 / 定期前向复盘

1. Discover
   - 查 Evidence Registry 是否已有相同主张
   - 检查数据与 A 股规则是否可支持
2. Specify
   - 冻结 Hypothesis Card、主要指标和证伪条件
3. Build
   - 在 Alpha DSL / Strategy API 内实现最小 baseline
4. Verify
   - 单测、防未来、PIT、walk-forward、成本、FDR/DSR/PBO
5. Challenge
   - 与简单基准、同族代表和风险暴露比较
6. Decide
   - reject / revise / candidate；不允许静默改门禁
7. Record
   - 追加 ledger、manifest、失败原因和下一步
```

### 自动停止条件

- 同一错误连续出现 3 次；
- 数据源缺失或时点无法证明；
- 主要结果只在已触碰 validation 上成立；
- 家族级多重试验后证据消失；
- 成本/不可成交后净 alpha ≤ 0；
- 与现有代表策略高重合且无组合边际贡献；
- 需要扩大到融券、杠杆、自动实盘等未授权范围。

### Maker / Checker 分离

- Maker：研究实现器生成特征/策略与实验。
- Checker 1：静态检查和单元测试。
- Checker 2：锁定数据切片与统计门禁。
- Checker 3：A 股规则/成交回放。
- Checker 4：人类只审批假设和晋级，不手工修改结果。

---

## 7. 近期可直接创建的任务清单

### P0

- [ ] 固定 pytest `testpaths`，把 vendored RQAlpha 测试独立出去。
- [ ] 完成当前策略删除与硬编码 AST 测试清单同步，保留 tombstone。
- [ ] 将 `status.json` 的“编辑状态提示”改为只允许 `pipeline.promote`/retire 命令。
- [ ] 增加 `paper_registry` 与 4 张 Paper Card：CausalStock、GPT-Signal、M6、Financial Statement Analysis（withdrawn）。
- [ ] 在 `execution_model` 前新增按日期查询的 `security_rule` 接口，先修科创板 200 股和 IPO 前五日规则。

### P1

- [ ] 为公告/财报 schema 增加 `published_at/available_at/revision_id/source_hash`。
- [ ] 写 100 个公告时点 golden cases。
- [ ] 建 Hypothesis Card + Alpha DSL v0（先支持横截面和财务算子）。
- [ ] 将 experiment ledger 从“策略名+params hash”升级为“家族+假设+数据+代码+参数”。
- [ ] 新增 FDR/PBO 与 locked-test 状态。

### P2

- [ ] 实现 S1 简单多因子 baseline，禁止先上 LLM/GNN。
- [ ] 建 Portfolio Lab 分解预测、配置和成本贡献。
- [ ] 实现分钟/集合竞价 L1 撮合（若数据具备）。
- [ ] S2/S3 仅在 PIT 文本和事件语料通过验收后启动。

---

## 8. 最终建议

论文对这个项目最有价值的，不是提供“41+48+若干 LLM 策略”，而是提供四个建设原则：

1. **关系和事件比单一情感更重要**：新闻必须落到公司关系、发布时间和影响衰减。
2. **预测和投资必须分开验证**：M6 已经说明高预测分数不自然产生高收益。
3. **LLM 应加速假设与抽取，不应绕开统计纪律**：GPT-Signal、QuantAgent、Alpha-GPT 的自动化只有在假设冻结、试验记账和独立 checker 下才有价值。
4. **复现状态高于论文名气**：已撤回的财报 LLM 论文说明，系统必须能够撤销依赖失效证据的所有晋级。

所以最有价值的系统改造不是继续扩大虚拟投研团队，而是把项目升级为一个能回答以下问题的 A 股研究操作系统：

> 这条信号在当时真的可见吗？它与已有信号有什么独立增量？在真实 A 股规则和容量下还能成交并保留收益吗？它是否在完全未触碰的数据上继续成立？

在这四个问题没有同时得到肯定答案前，策略应停留在 research/Shadow；得到答案后，也只获得申请下一阶段的资格，不自动获得实盘权限。
