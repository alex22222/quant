# 改进计划（基于 PROJECT_REVIEW 复核后制定）

> 日期：2026-08-23
> 依据：`docs/PROJECT_REVIEW.md` + 当日代码复核
> 原则：**先止血、再修账本、再修门禁，最后才谈 LLM 团队。** 每个阶段都有明确验收标准，不达标不进下一阶段。

## 复核结论：审查指认的问题当前仍然成立

| 审查指认 | 复核结果 |
|---|---|
| 决策事实源分裂（8-22 三源冲突） | ✅ 属实。`decisions/2026-08-22/decision.json` 全部"观望"+`market_ok=false`，但 `loops/approvals.json` 同日 5 项全部 approved，`outputs/.../plan.json` 又含平安/宁德买入计划 |
| `stage_paper.py` 硬编码策略 | ✅ 属实。`UNIVERSE / MOM_DAYS=20 / HOLD_NUM=3 / MA_DAYS=120` 写死在 20-24 行，只查注册表 state=="live"，从不加载策略文件 |
| 收盘价成交、免费用、无 T+1 | ✅ 属实。`stage_paper.py` 与 `execute.py` 注释中自述为 v1 简化 |
| 仓位字符串解析风险 | ✅ 属实且比审查写的更糟：`plan.json` 的 `position_pct` 是 `"15%（8%+7% 分批）"` 这种自由文本，而 `execute.py:138` 直接做 `pos_pct > 0` 数值比较——一旦 execute 改读 plan.json 或决策源混入字符串，立刻 TypeError |
| 审批与执行脱节 | ✅ 属实。`approvals.json`（console 审批）与 `decision.json` 的 `pm_status` 是两条独立写入路径，`execute.py` 只看 `pm_status`，console 批了什么都不影响执行 |

## Phase 0 · 止血（1-2 天，纯防御性改动）

> 目标：杜绝"批了没执行/没批却执行/多源打架"的静默错误。

1. **冻结自动执行链路**
   - `execute.py` 加 `--confirm` 显式开关，无开关只 dry-run 打印计划不写库。
   - 定时任务/console 里的自动触发入口暂时禁用。
2. **指定唯一主事实源：`decisions/YYYY-MM-DD/decision.json`**
   - `plan.json` 降级为"提案草稿"（trader 输出，不可执行）。
   - `approvals.json` 降级为"审批操作日志"，不再是判断依据。
   - 审批动作（console/飞书）必须**回写 decision.json 的 `pm_status` 和每条 plan 的 `approved` 字段**后才生效。
3. **写一致性校验器 `trading_team/consistency_check.py`**
   - 同日三源逐条比对：方向、position_pct、market_ok 约束。
   - `market_ok=false` 时任何 `position_pct>0` 的计划不得进入 approved executable 状态。
   - 不一致 → 打印冲突清单并 exit 1（fail closed）。接入 `execute.py` 前置步骤。
4. **结构化 plan schema**
   - 新 schema：`{code, direction, position_pct: float, entry: [float,...], entry_type, stop: float, condition: str}`。
   - 写迁移脚本把 8-22 的字符串仓位解析一次，解析失败的人工确认。
5. **修 `execute.py:138` 的类型隐患**：`position_pct` 统一走 `parse_pct()`（接受 float/int，拒绝字符串并报错）。

**Phase 0 验收**：故意制造一份三源冲突的 decision/plan/approvals，校验器必须拒绝执行；`market_ok=false` 时买入计划物理上无法被标记 approved。

## Phase 1 · 可信账本（约 1 周）

> 目标：Paper 账户的每一笔交易都可复盘、可解释、可复现。

1. **统一信号接口 `strategies/base.py`**
   ```python
   def generate_targets(as_of, portfolio, data, params) -> list[Target]
   ```
   - 先迁移 `momentum_rotation` 一个策略作为样板，其余策略后续逐个迁移。
   - 回测（rqalpha 侧）与 Paper 共用同一函数。
2. **重构 `stage_paper.py`**
   - 删除硬编码 `UNIVERSE/MOM_DAYS/HOLD_NUM/MA_DAYS`。
   - 从 `status.json` 注册表找到 live 策略 → import 其模块 → 调 `generate_targets()`。
   - stage_paper 只负责：加载策略、取账户、撮合、记账。
3. **A 股撮合约束（`paper/execution_model.py`）**
   - T 日收盘出信号，T+1 成交；买入价默认 T+1 开盘价（无开盘价数据时 VWAP 近似，需注明）。
   - T+1 卖出限制、整手、涨跌停不可成交、停牌跳过。
   - 费用模型：佣金万 2.5（最低 5 元）、印花税卖出千 1（以当前政策为准并写成配置项）、过户费。
4. **trade 记录扩展字段**：`signal_source, data_version, planned_price, fill_price, reject_reason, risk_flag`。旧表写迁移脚本加列。
5. **止损三套口径并行记录**：收盘止损 / 次日开盘止损 / 跌停延迟止损，复盘报告对比三者差异。

**Phase 1 验收**：同一段历史区间，回测与 Paper 用同一信号函数跑出的目标持仓序列完全一致；Paper 重跑幂等；每笔 trade 能回答"谁给的信号、哪个版本数据、计划价多少、实际成交价多少、为什么没成交"。

## Phase 2 · 重做门禁（约 1 周，可与 Phase 1 并行后半段）

1. **样本内/样本外拆分**：默认 70/30，OOS 不达标一票否决。
2. **滚动窗口回测**：至少 3 个市场阶段（上涨/下跌/震荡）分别统计。
3. **参数扰动**：动量窗口 ±25%、持仓数 ±1，结论不得反转。
4. **成本模型进回测**：与 Phase 1 撮合模型共用同一费用配置，禁止两套口径。
5. **指标体系**：超额收益（对沪深300）、信息比率、最大回撤、最长回撤恢复期、最差单月、最差连续亏损、换手率、持仓集中度、行业暴露。
6. **策略族去重**：`momentum_rotation` 与 `momentum_stops(stop=none)` 合并为同一族；注册表加 `family` 字段，同族只允许一个代表进 live 评审。
7. **复盘报告改格式**：必须展示被拒策略及拒绝原因，不只展示通过的。

**Phase 2 验收**：现有全部 live/候选策略用新门禁重跑一遍，输出过/拒清单；预期大部分现有策略会被拒——这是预期结果，不是 bug。

## Phase 3 · LLM 团队降级定位（Phase 1 完成后）

1. **三层输出**：
   - `research_signal`（分析师观点）→
   - `trade_proposal`（候选计划，结构化 schema）→
   - `executable_order`（确定性风控校验后的模拟订单）。
2. **只有第三层能写 Paper 账户**。LLM 任何输出不得直接碰 `paper.db`。
3. 风控规则（仓位上限、market_ok、黑名单、单日最大交易数）用确定性代码实现，LLM 只能"建议"，不能"绕过"。

## Phase 4 · 测试体系（贯穿始终，Phase 0 起步就写）

用 pytest，优先覆盖审查列出的关键路径：

- `tests/test_consistency.py`：三源冲突 → 拒绝
- `tests/test_approvals.py`：审批状态流转、同日重复审批覆盖、approved/rejected/reduced
- `tests/test_execute.py`：方向解析（买入/卖出/持有/观望/回避）、position_pct 解析、现金不足调整、positions 重写前后权益守恒、幂等
- `tests/test_gate.py`：门禁拒绝逻辑
- `tests/test_data_patch.py`：patched bundle dry-run、重叠日校验、拒写逻辑

## 数据层整改（Phase 1 期间顺带做）

1. 四层数据明确命名：`raw_external_context` / `official_bundle` / `patched_bundle` / `paper_prices`，禁止静默混用。
2. `patch_bundle_from_context.py` 加：版本号、校验摘要、写入前校验（重叠日收盘价、复权口径、涨跌停字段、量价单位、dtype），输出校验报告。
3. `paper_prices` 单独落库，Paper 撮合只读它。

## 执行顺序与时间估算

```
Week 1:  Phase 0（止血）+ 测试骨架
Week 2:  Phase 1（信号接口 + 撮合约束）
Week 3:  Phase 2（门禁）+ 数据层整改收尾
Week 4:  Phase 3（LLM 降级）+ 全链路联调
```

硬性依赖：Phase 0 完成前**不得**新增任何 LLM 角色、推送、console 功能；Phase 1 完成前**不得**让任何 LLM 提案进 Paper 账户。

## 不做清单（本周期明确排除）

- 不新增 LLM 角色 / 不新增飞书推送样式 / 不做 console 新页面
- 不引入新数据源
- 不优化策略本身的 alpha（先把度量修对，再谈策略好坏）

---

## 执行记录（2026-08-23 完成）

### 已完成

**Phase 0 止血（全部完成，实测验证）**
- `trading_team/consistency_check.py`：三源一致性校验器，exit 1 fail closed。8-22 实测检出 7 项冲突
- `trading_team/schemas.py`：`parse_pct`/`parse_entry` 严格解析（"15%（分批）"类文本直接 SchemaError）、`compute_executable` 单票门禁、`validate_portfolio` 组合级风控（单票 ≤25%、买入合计 ≤95%、单日 ≤5 笔、market_ok=false 禁买入）
- `trading_team/approvals.py`：批复回写主事实源 decision.json（per-plan approval/executable/blocked_reason + pm_status 重算）；approvals.json 降级为日志
- `trading_team/execute.py`：默认 dry-run、`--confirm` 才写库、前置一致性校验、per-plan executable 门禁、reduced 减半、现金不足整手下调、幂等
- `trading_team/auto_execute.py`：默认冻结（dry-run），需 `QUANT_AUTO_EXECUTE=1` 才自动执行
- 8-22 数据修复：审批回写主事实源、plan.json 由 decision.json 重建（原草稿备份于 `backups/2026-08-23-phase0/`）

**Phase 1 可信账本（核心完成）**
- `strategies/base.py`：统一信号接口 `generate_targets(data, params)` + `SignalResult` + `BundleDataAdapter`（并行会话后续扩展了 ohlc/FREQUENCY/positions，兼容）
- `momentum_rotation.py`：信号核心抽出为纯函数 `compute_targets()`，rqalpha 与 Paper 共用；`momentum_stops.py` 同族复用，PARAMS 唯一来源
- `paper/execution_model.py`：T+1 开盘成交、涨跌停/停牌拒单、整手、T+1 卖出限制、费用模型（佣金万2.5最低5元+印花税万5+过户费万0.2）、止损三口径对比 `compare_stop_fills`
- `pipeline/stage_paper.py` v2：注册表加载 live 策略（多个 live 直接报错 fail closed——实测拦下了 turtle_bluechip 与 momentum_stops 同时 live 的冲突）、挂单-成交分离（信号日 T → T+1 撮合）、trades 扩展字段（signal_day/signal_source/data_version/planned_price/fill_price/fee/reject_reason）、幂等。旧台账归档 `backups/2026-08-23-phase0/paper_v1.db.bak`

**Phase 2 门禁 v2（完成，实测验证）**
- `pipeline/gate.py`：全样本+OOS 双达标、超额收益/信息比率/换手/最长回撤期门槛、参数扰动不得反转、策略族去重
- `pipeline/stage_backtest.py`：全样本 + OOS（2024 起）+ 扰动变体 + 3 个市场阶段统计 + 月度指标（最差单月/月胜率，从 portfolio.csv 计算）；成本口径 pit-tax 历史印花税 + 滑点 0.2%
- 实测：momentum_rotation 旧门禁通过（年化 10.4%），门禁 v2 **拒绝**（OOS 超额 -4.2%、扰动回撤 38-47%、最长回撤 1707 天）——验证了审查关于"虚假信心"的判断
- `pipeline/stage_review.py`：复盘报告展示拒绝原因、OOS、扰动、阶段窗口、月度分布、挂单与成交明细

**Phase 3 LLM 降级（完成）**
- 三层契约写入 `prompts/06_trader.md`、`07_risk_manager.md`：research_signal → trade_proposal → executable_order；LLM 输出不得直接写 Paper；trader 必须输出结构化 JSON schema

**Phase 4 测试（完成）**
- `tests/` 98 项 pytest 全部通过：schemas/consistency/approvals/execute/gate/execution_model/stop_fills/data_patch/portfolio

**数据层（完成）**
- `patch_bundle_from_context.py`：量价单位自检（>30% 偏差拒写）、涨跌停字段自检、dtype 记录、patch_manifest.json 版本化 + 校验摘要、拒写时 exit 1。实测拦下紫金矿业重叠日 1.275% 偏差（除权未对齐）

### 遗留（诚实清单）

- `paper_prices` 未单独落库：Paper 仍经 adapter 直读 bundle（已有 manifest 版本化缓解）
- 止损三口径工具已实现，但未接入 live 流程（Paper 暂不支持 stop≠none 变体）
- ~~8 个 candidate 策略的门禁 v2 全量重跑未执行~~ → 已于 2026-08-23 下午重跑（见下方执行记录二）
- `execution/` 模块为并行会话（alex22222）的另一套执行栈，与 `paper/` 的关系需要人工对齐
- ~~turtle_bluechip 已从 live 降回 candidate~~ → 已于 2026-08-23 下午攻关后重新晋升 live（见下方执行记录二）

---

## 执行记录二（2026-08-23 下午：门禁 v2 全量重跑 + turtle_bluechip 攻关）

### 门禁 v2 全量重跑

- 8 个策略全部重跑，**全拒**（报告在 `reports/gate_v2/summary.md`，本地不入库）
- 结论处理：`momentum_stops` 从 live 降为 candidate（commit `0f344ef`），当前无策略靠旧成绩留在 live

### turtle_bluechip 攻关（candidate → live）

**新增参数**（`strategies/turtle_bluechip.py` PARAMS）：
- `ma_slope_days`（均线斜率过滤，0=关）
- `chandelier_atr` / `chandelier_days`（吊灯止损，0=关；18 组实验证明无益，最终关闭）
- `equity_filter_days=120` / `equity_filter_scale=0.5`（**权益曲线滤波，核心改进**：自身净值跌破 120 日均线时仓位减半）
- `bull_band=0.02`（强牛豁免带）
- `ma_days` 从 120 调整为 60

**获胜组合 E18 指标**：
- 全样本：年化 13.3% / 最大回撤 23.5% / 夏普 0.67 / 超额 +11.3% / 最长回撤期 1359 天
- OOS（2024-01 起）：年化 13.0% / 超额 +0.5%
- 8 组参数扰动全部不反转

**门禁语义升级**（`pipeline/gate.py`）：
- 扰动判定从"指标同样达标"放宽为"**结论不反转**"（扰动版允许指标下滑，但不允许超额转负等方向性恶化）
- 回撤容差带 ×1.2；年化/夏普/超额仍严格
- `_perturb_variants` 上限 6→8，新增 n_entry/n_exit 扰动规则

**Paper 管线补齐**（`pipeline/stage_paper.py`）：
- 日线策略支持（配合并行会话的 FREQUENCY 判断）
- 补 nav_hist 传递 + 权益滤波后的 weight_cap 生效（用 inspect.signature 按能力传参）
- 实测 2026-08-21 market_ok=false 正确空仓

**验证与交付**：
- 98 项 pytest 全部通过
- turtle_bluechip 晋升 live（status.json）
- commit `4e16f02`（7 files changed, 168 insertions），已推送 GitHub main

---

## 执行记录三（2026-08-23 晚：策略库审查整改，Loop Engineering 方式）

依据 `docs/STRATEGY_LIBRARY_REVIEW_A_SHARE.md`（同日审查，认定上午的 turtle 晋级方法论不成立），按"改 → 跑校验 → 看结果 → 修"闭环逐项整改：

### P0 治理（commit f9e76ae）
- turtle_bluechip live→candidate，E18 参数冻结（OOS 污染 + 成交口径不一致，晋级证据作废）
- 2024-2026 正名为 validation（非独立 OOS），`gate.py DATA_PARTITION` 统一声明；复盘/测试文案同步
- `pipeline/promote.py`：策略状态变更唯一入口，原子写入晋级证据（策略/参数/门禁 hash + 数据版本 + 报告路径 + 批准人）；晋级 live 硬性前置最新门禁 pass
- `pipeline/stage_audit.py`：注册表↔门禁一致性自检自愈环路（接入 pipeline 默认阶段）；实测模拟脏编辑 live → 自动降级 + exit 1

### P1 成交口径（commit c959297）
- 探针实验证实：handle_bar 日线订单按**当日收盘价**成交（信号价=成交价）；open_auction 阶段订单按**次日开盘价**成交（对照 bundle 逐笔验证）
- 7 个日线策略统一改造为 T 日信号 → T+1 开盘集合竞价执行，与 Paper 收益口径一致
- T+1 口径门禁重跑：8 策略全拒。turtle 验证集超额 +0.5%→**-0.7%**，证实原晋级结果部分来自成交时点假设

### P1 测试与 Paper（commit ac6b72a）
- `tests/test_turtle_core.py` 20 项：审查清单 8 类策略核心行为（含 AST 源码守卫：下单只允许出现在 open_auction）
- stage_paper 挂单顺延重试：涨停/跌停/停牌/T+1 限制保留 pending 顺延（上限 5 日作废），逻辑性拒单立即作废；schema v2→v3 含旧库迁移
- `tests/test_paper_retry.py` 4 项

### P3 策略库精简（commit d2fa97d）
- rotation_300_500 / rsi2_reversal / momentum_stops → retired；美股财报反转策略移至 research/
- 状态变更全部走 promote.py（dogfood）。当前库：5 candidate / 0 live / 5 retired

### P2 门禁多重试验修正（commit f9309ac）
- 简化 Deflated Sharpe Ratio（Bailey & López de Prado）：试验簇夏普方差由扰动变体估计，DSR < 0.95 拒绝，试得越多门槛越严
- `pipeline/experiment_ledger.jsonl` 实验台账落 git（params_hash 去重计数，"试了多少次"可复现）
- 冒烟实测：turtle DSR=0.9999（该项通过），仍因验证集超额被拒

### P2 股票池一期（commit 2e5bee8）
- `pipeline/universe.py`：point-in-time 可投资池（上市满 1 年 / 当日非 ST / 未停牌 / 20 日日均成交额 ≥5000 万，取前 100），消除固定 10 只蓝筹事后选择偏差；已退市股随数据终止自然排除
- 81 期月度快照（2019-12~2026-08，每月首个交易日）落 git 可复现
- `tests/test_universe.py` 10 项

### 验证
- 全量 134 项 pytest 通过

### 遗留（更新）
- 股票池快照尚未接入策略（turtle 参数冻结中；新策略须以快照池走 research→candidate 门禁）
- 股票池/信号/择时收益归因分离未实现（审查 P2 第 4 条）
- 回测收益归因（信号收益/开盘跳空/费用/滑点/未成交）未实现
- 撮合模型仍缺：盘口封单量、部分成交、集合竞价冲击成本（审查"撮合适配"节）
- turtle 再晋级路径：T+1 口径门禁通过 + 6-12 个月纯前向 Paper + promote.py 原子晋级

---

## 执行记录四：Quant-Wiki 八条改进建议落地（2026-08-23/24）

来源：`docs/improvement-suggestions-quant-wiki.md`（quant-wiki 四文对照后的 8 条建议）。
建议 8（绩效口径统一）按文档自身约定暂缓——等 8-28 首批 accuracy 数据后再做。

### 建议 1+5（commit ea9c0b8）
- prompts 01-05/07 末尾强制 ```json 结论块契约（rating 七档枚举 + confidence 1-5，块后禁正文）；03 含逐条打分 + temperature；07 含 reviews 决策块
- 06/07 写入 5 条不可协商风控条款（单票 25% / 止损距入场 ≤15% / 现金 ≥5% 单日 ≤5 笔 / market_ok 禁买入 / 信心度 ≤2 减半）
- `trading_team/conclusion_block.py`：extract_block（取最后一个 json 围栏块、按角色校验、fail closed）+ backfill_accuracy（幂等回填 loops/accuracy.json，已 verified 不覆盖）

### 建议 2+3（commit 063c4f3）
- `collect.py` 指标扩充：Stochastic %K/%D（14/3）、VWAP 20 日、布林带（20,2，收口 pct_b=None fail closed）；prompt 04 同步（旧 context 文件无新字段，下次采集生效）
- `conclusion_block.py` 新增 news_temperature + enrich_plan：舆情温度与情绪周期幂等写入 plan.json（只追加只读上下文字段）；main() 子命令化 backfill|enrich

### 建议 6（commit f94e2e0）
- `pipeline/promote.py`：入 candidate 硬性前置 --differentiation 因子区分度说明（与在库策略相关性/增量逻辑），缺失即拒绝并列出在库策略对照；AGENT.md 工作纪律第 7 条成文
- 样本外验证部分门禁 v2 已有（validation + DSR），不重复建设

### 建议 7（commit fdf3259）
- `trading_team/README.md` 新增「团队纪律（LLM 局限性护栏）」7 条：数据缺口标注 / 事实判断分离 / 单角色不单独成计划 / 准确率降权 / 结论块 fail closed / 防 HARKing / LLM 输出必须经风控+人工批复

### 建议 4（commit 6a5e0d7）
- `pipeline/research_ingest.py`：quant-x-monitor 报告 → 关键词分类（trading_signal/risk_management/噪音）→ 命中 signal 按关键词选型模板生成统一接口策略草案（research 状态入库，stage_strategy 自动扫描）
- 幂等去重（ledger.jsonl 按 URL）、单次草案封顶 5、--dry-run；实测 8-23 摄取生成 4 份草案
- Loop #4 挂起的"研究线索沉淀"正式打通

### 验证
- 全量 174 项 pytest 通过（新增 test_conclusion_block 8 项 / test_indicators 4 项 / test_promote 6 项 / test_research_ingest 11 项）

### 遗留（新增）
- 4 份 ingest 草案待人工核实逻辑后走 promote --differentiation 晋级评估（当前均为 momentum 模板兜底，区分度存疑，大概率应退役或改写）
- enrich_plan/backfill 需在每日日报 Automation 提示词中接入调用（当前为手动 CLI）
- 建议 8：等 8-28 首批 accuracy 数据后统一绩效口径
