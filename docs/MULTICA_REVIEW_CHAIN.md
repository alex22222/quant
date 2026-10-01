# 多模型串行审阅器

提交范围：此分支保存多模型协调器增量，尚不能在当前 main 基线上独立运行。它依赖本地已有、尚未入库的版本化研究/Paper 基础层，包括 `kimi_daily.py`、`multica_adapter.py`、`multica_reviews.py`、`run_ledger.py` 及其传递依赖；测试还依赖已有研究与审阅夹具。基础层整合前不应合并为可运行版本。下面的验收结果来自包含这些依赖的完整本地工作区。

`trading_team.multica_review_chain` 对已经合格发布的同一版本研究包，依次派发数据质量、市场分析、策略研究、模拟交易提案、风控和审计六个现有 Multica 角色。它沿用 `multica_reviews` 的输入包、真实任务身份和不可变回执校验。

## 调用与范围

默认只检查当前来源及审阅状态，不派发任务：

```bash
.venv/bin/python -m trading_team.multica_review_chain --date YYYY-MM-DD --team /absolute/path/trading_team
```

用户授权执行后，加 `--execute`，并在 Multica 任务之外运行协调器：

```bash
.venv/bin/python -m trading_team.multica_review_chain --date YYYY-MM-DD --team /absolute/path/trading_team --execute
```

协调器不能在一个运行中的 Multica Agent 内再扇出任务。它使用现有登录的 Multica CLI，先等待一个角色结束并核对当前有效回执，再派发下一个角色。角色仅能写自己的响应，通过标准 submit 命令记入所选 team 的审阅账本，并更新自己的 Issue。

入口不生成日报、不改报告正文、不批准交易、不结算 Paper、不发送通知，也不修改或启用定时任务。生产定时发布入口与本审阅入口的调度连接需要单独验收；一次隔离链通过不能被描述成下一次自然日流程必然成功。

## 验收条件

每个阶段必须同时满足：远端执行 completed、Agent/runtime 匹配、实际模型用量不存在冲突、当前同版本审阅 accepted，以及回执的 Issue/task 与本轮远端执行完全一致。远端 completed 没有回执时停止。Kimi ACP 可能不提供模型用量，此时记录 `configured_runtime_only`，不能伪造 token 数或声称拿到了服务端模型标签。

拒绝、缺证据、来源变化、模型冲突、任务失败或超时都会阻断下游。失败产生的平台自动重试会被停止。重跑同一命令会读取既有 state，复核相同任务和回执；不会重复派发已经绑定的 Issue。发现同标题但没有持久绑定的 Issue 时停止，防止创建响应丢失后重复派发。阻塞状态需要先调查，不能自动抹除再跑。

远端状态查询发生 CLI 错误时，最多连续尝试三次，只重读状态，不重发任务；错误保存在对应角色的 `poll_errors`。持续失败会退出并保留恢复位置。再次运行同一命令可继续观察原任务。

状态、各角色响应、任务说明和汇总位于所选 team 的：

```text
.runs/multica/review_chains/YYYY-MM-DD/state.json
.runs/multica/review_chains/YYYY-MM-DD/report.md
```

汇总逐项展示角色、模型、Issue、结论与理由。所有结果保留 `execution_authorized=false` 和 `production_complete=false`。

## 本次验证

测试先复现缺少执行器，随后覆盖完整串行交接、幂等重跑、拒绝后停止、缺回执、失败任务、模型冲突、重复 Issue、其他活动任务、超时取消及输入版本变化。真实多模型验证使用明确标记的合成来源，生成器为 `offline_fixture.v1`；审阅角色通过真实 Multica 身份调用 GPT、Claude、Kimi。不得将合成行情或固定生成器描述为真实市场研究或模型生成效果。

2026-10-01 已完成六角色真实串行调用、同任务恢复及完成后幂等重跑；最终完整回归 1,293 项通过。结论和限制见 [验收报告](MULTICA_CHAIN_VALIDATION_2026-10-01.md)。
