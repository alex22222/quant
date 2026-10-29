# LLM 计划到 Paper 的版本化执行链

## 主链路

1. LLM 只返回研究与提案；确定性 runner 校验后发布 `trading_team/outputs/YYYY-MM-DD/plan.json`。
2. `trading_team.daily_finalize` 规范化计划并计算 SHA-256 `plan_hash`，生成
   `trading_team/decisions/YYYY-MM-DD/decision.json`。
3. 审批请求必须携带当前 `plan_hash`。计划的方向、仓位、价位、止损、条件或
   `market_ok` 变化后哈希改变，旧批复自动失效为 `pending`。
4. Paper 执行器同时校验 `decision.plan_hash` 和每条计划的
   `approved_plan_hash`，不匹配时 fail closed。

## A 股成交口径

- 信号和执行日分离，按 T+1 行情撮合。
- 限价单使用执行日 OHLC 严格判断是否触发；未触发记 `unfilled`。
- 突破单只有当日最高价达到触发价才可成交。
- 买入数量按 100 股整手向下取整。
- 包含佣金最低 5 元、印花税、过户费和默认 0.2% 不利滑点。
- 停牌、开盘涨停买入、开盘跌停卖出和 T+1 当日回转均拒绝成交。

## 每日输出

每日收尾与索引已经拆分，避免索引命令隐藏业务副作用：

```bash
.venv/bin/python -m trading_team.daily_finalize
.venv/bin/python -m trading_team.build_index
```

`daily_finalize` 会按计划版本写入运行账本：

- 物化/刷新所有 `decision.json` 并同步 `team.db`。
- 对已收盘交易日生成 `counterfactual.json` 和 `counterfactual.md`。
- 同时生成 `continuous_counterfactual.json/.md`，沿用两组合现金与持仓；缺口中断，全区间收益未知。
- 反事实固定对比「闸门组合 / 无闸门组合 / 沪深300」。
- 行情快照缓存在 `trading_team/replay_data/YYYY-MM-DD.json`，便于重放复现。
- 同一 `plan_hash + replay/paper 模式` 二次执行直接复用；失败重试只继续未成功步骤。

`build_index` 现在只重建 `outputs/index.json`。兼容性批处理必须显式使用
`--finalize-available`，不能再把“索引成功”当作 Paper 已成功。

可单日手动运行：

```bash
.venv/bin/python -m trading_team.daily_finalize 2026-08-25
.venv/bin/python -m trading_team.counterfactual 2026-08-25
```

Paper 执行默认仅预览，显式确认才写库：

```bash
.venv/bin/python -m trading_team.execute 2026-08-25
.venv/bin/python -m trading_team.execute 2026-08-25 --confirm
```

## 自动 Paper 实验

从 `2026-09-03` 起，日报同时物化独立的 `paper_decision.json`，不修改 Live
的人工审批状态：

- `paper/llm_gate.db`：市场闸门组合；闸门关闭时不买入，不会自动退出既有持仓。
- `paper/llm_no_gate.db`：无闸门对照组合，仅绕过市场闸门，不绕过其他风控否决。
- Paper 审批自动绑定当前 `plan_hash`；计划变化后必须重新物化，旧版本拒绝执行。
- 单票目标仓位上限 `10%`，每日最多选择前三笔合规买入候选。
- 两个账户继续执行严格限价触发、T+1、费用、滑点、整手、停牌和涨跌停模型。
- `data_asof == date` 的收盘后补报只标记为 `same_day_retrospective`，禁止倒填成交。

Live 的 `decision.json`、人工审批和硬闸门规则保持不变。

## 显式退出与持仓语义

2026-09-22 起，新执行绑定 `a_share_exit.v1` 与 `explicit_intent.v1`；旧回执不得静默复用。
回避、观望、持有、候选缺席及买入被否决均不是卖单。每个标的每天一条意图，拒绝重复。

- 卖出：`position_pct=0`、`exit={"type":"market","qty":"all","time_in_force":"DAY"}`。
- 减持：股数为正整数整手，严格小于现有持仓；不自动缩量或转换成清仓。
- 限价/止损：`exit.type=limit/stop`，另带分单位 `price`；市价单不得带价格。
- 退出沿用结构化开盘条件，T+1、费用、滑点、停牌、涨跌停；未触发为 `unfilled`。
- 缺可核验库存拒绝退出；`fifo_acquisition.v1` 按成交账本重建逐批可卖量，当日加仓不冻结旧仓。
- 部分减持不得超过可卖量；全卖含冻结股时拒绝，不静默缩量。成本仍为加权平均，详见 [PAPER_INVENTORY.md](PAPER_INVENTORY.md)。
- 退出不受三笔买入筛选上限影响；风险否决退出只移除指令，不改变持仓。
- 当日卖出所得不参与当日买入预算，避免日线无法证明先卖后买的时序；不是市场资金规则。
- 显式 DAY 止损不跨日延续，也不是买入后自动保护单；买入 `stop` 字段仍仅供风控参考。
- 已有双账户 Paper 持仓可单独使用版本化 `protection` 指令；来源、撤销/替换、有效期和逐日回执必须匹配，见 [PAPER_PROTECTION.md](PAPER_PROTECTION.md)。

公开 Paper 入口同时核对当前计划内容、日期、两组合闸门和每笔审批/可执行状态，
不能仅凭相同的顶层 `plan_hash` 或旧 `executable=true` 执行。退出价、类型、股数变化须重新审批。
只读审计用交易账本重建期初持仓并与回执比较；缺证不补猜，不认证旧成交。

标准每日反事实仍是现金重置实验，不是连续 Paper 账户收益。显式传入期初持仓的对照才
模拟已有库存，缺库存时退出被拒绝。未成交退出不产生虚构交易，但既有持仓仍需逐日估值。
新增连续报告是独立内存账户的模型实验，不能认证旧成交或前向发布时间。收尾缓存同时
复核连续报告、历史输入和运行时代码；详见 [CONTINUOUS_COUNTERFACTUAL.md](CONTINUOUS_COUNTERFACTUAL.md)。

## 结构化风控与历史隔离

2026-09-22 起，新日会使用 `daily_research.v3`，新自动执行绑定 `structured_risk.v1`。
`a_share_risk.v1` 计划同时保存原始 `trader_proposals`、逐条 `risk_reviews`、最终 `proposals`
和实验 `paper_proposals`，全部纳入 `plan_hash`。物化及公开执行入口重新验证派生结果，
不能只改 `paper_proposals` 或复用旧审批来绕过风控。

- 原始候选不能自带审批/风控字段；审核按代码完整、一对一覆盖，重复与遗漏均拒绝。
- 通过的 `reason_codes=[]`；降仓和否决必须有非空的已知原因码及解释。
- 仅 `否决 + ["market_gate"]` 可在无闸门对照中恢复原始买入，且原始确定性闸门必须关闭。
- 混合风险否决不恢复；降仓严格低于原仓位并保留价位和全部条件；退出不允许百分比降仓。
- `reason`、`clause` 仅供说明，文字中出现“条款4”或 `market_ok=false` 不产生授权。
- 缺少结构化审核的旧计划仍可查看，新物化保持 `pending`、没有批准哈希、不可自动执行。
  不改原文件、不补签旧审核、不撤销或重写历史账务。版本匹配不等于审批通过。
- 旧计划的反事实标记 `unreviewed_hypothesis_no_auto_paper`，只能解释假设意图；
  即使假设成交，也不是自动 Paper 获批或前向业绩。新合规计划的同源撮合可逐笔比较。

风控响应和日报总结均在记录角色成功之前校验；失败原响应留在 `.runs/rejected/`，
重试复用有效上游角色，只重做失败审核。角色输入包括规范化原始提案，避免仅绑定文字报告。
结构化审核证明可追踪和一致性，不证明模型判断正确或建议能盈利。
