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
