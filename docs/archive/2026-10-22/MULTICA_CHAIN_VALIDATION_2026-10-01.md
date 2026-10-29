# 多模型角色与审阅链验收（2026-10-01）

最终核验：2026-10-01 21:40:21（Asia/Shanghai）。两项验证完成：Kimi 两个角色复测通过；真实 GPT、Claude、Kimi 六级审阅在隔离合成样本上通过，完成中断恢复及幂等重跑检查。

这是接入、交接与审阅机制验收，不是投资效果或真实交易日验收。现有定时发布任务没有接入新增协调器，不能据此承诺下一份定时日报自动完成三家模型联合审阅。日报正文的现有生成路径仍使用 Kimi。

## 1. Kimi 角色复测

| 角色 | 原 Issue | 本次真实 task | 结果 |
| --- | --- | --- | --- |
| Data Quality | SATU-67 | `01a0f78e-e28e-74a6-bc00-9ca11831f40f` | completed；正确接受正常案例、拒绝错误案例；识别实际覆盖率 0.7 |
| Paper Trader | SATU-70 | `01a0f791-3120-759c-b3a0-4e98bfd140f9` | completed；正确接受正常案例、拒绝错误案例；识别超卖 100 股 |

两个角色的输入 SHA、随机标识、角色、计算结果、拒绝理由和 `execution_authorized=false` 全部独立核对通过，真实 Agent/runtime 与分配一致。原认证失败历史保留，原 Issue 现为 done。连同先前通过的 GPT 两角色、Claude 三角色，七角色基础任务均有通过证据。

Kimi 配置为 `kimi-code/k3-256k`，实际由登记的 Kimi runtime 执行。但 ACP 返回 `usage=null`，本次没有服务端模型标签或 token 用量；模型证据标记为 `configured_runtime_only`，不伪造计费或模型确认。

## 2. 六级串行审阅

协调器：`trading_team/multica_review_chain.py`。默认只读，显式 `--execute` 才派发远端任务。每个阶段完成后，核对真实任务身份、同版本输入、接受回执及上下游依赖，再进入下一角色。

| 顺序 | 角色 | 配置模型 | Issue | 实际执行耗时 | 回执 |
| --- | --- | --- | --- | --- | --- |
| 1 | 数据质量 | Kimi K3-256k | SATU-74 | 417 秒 | accepted |
| 2 | 市场分析 | Claude Sonnet 5 | SATU-75 | 162 秒 | accepted |
| 3 | 策略研究 | Claude Opus 5.5 | SATU-76 | 136 秒 | accepted |
| 4 | 模拟交易提案 | Kimi K3-256k | SATU-77 | 318 秒 | accepted |
| 5 | 风控 | Claude Opus 5.5 | SATU-78 | 110 秒 | accepted |
| 6 | 审计 | GPT-5.5 | SATU-79 | 216 秒 | accepted |

六个 Issue 均为 done，每个恰有一次 completed 执行；六个 task 各不相同，前一任务完成时间不晚于下一任务开始时间。全部必需检查为 pass，来源、输入包与回执哈希有效。Claude/GPT 的实际用量模型标签与配置一致；Kimi 的证据限制见上节。完整时间线从 21:12:34 至 21:38:45，含交接及恢复等待共 1,571 秒，约 26 分 11 秒，不能据此保证未来运行时延。

### 样本与结论边界

- 输入位于 `/private/tmp/quant-multimodel-chain-l94kumxc/trading_team`，业务日期 `2026-09-21` 仅为测试键，不代表当日真实市场研究。
- 生成器是确定性的 `offline_fixture.v1` / `offline-test`，不是本轮真实 Kimi 报告生成，也没有独立 Research Lead 生成任务。真实调用仅用于六个审阅角色。
- `acceptance_scope.json` 明确标注合成数据、非真实行情、非真实账户证据；样例仅包含观望和零仓位意图。归档中的 `rqalpha_bundle` 是解析格式，不能当成真实 RQAlpha 行情来源。
- 六份接受回执认证的是声明范围内的一致性，不证明研究准确率、投资收益、真实账户约束覆盖或 M2/M3 自然交易日验收。

### 中断、恢复与重复运行

第一次执行在读取策略审阅状态时遇到 Multica CLI 的 TLS 握手超时，协调器退出；远端策略任务已经完成。错误独立保存在验收证据中，没有删除或改写失败记录。重新执行同一命令后，协调器复用前三个已绑定任务，继续完成后三阶段。

针对这个实际故障，先用两项测试复现，再加入仅针对状态读取的有界重试：最多连续三次，保存 `poll_errors`，不重新派发任务；持续失败保留恢复位置并退出。该自动重试行为由故障注入测试验证，本轮实际网络中断通过重新执行协调器恢复。

整链完成后再次实际执行同一命令，仍为 accepted。六组 Issue ID、task ID、receipt hash 与第一次完成时完全一致；各 Issue 仍只有一次执行，没有重复派发。

## 代码、边界与保护验证

- 新增协调器及行为测试，沿用已有不可变输入包、`submit_review` 和账本校验，不另造审阅协议。
- 来源失效、拒绝、缺失回执、错误模型、身份或版本变化、已有同名未绑定任务、超时和执行失败都停止下游；测试覆盖恢复与重复运行。
- 对现有 `2026-09-29` 日报执行只读入口检查，得到 `blocked / source_unverified`，没有派发远端任务；未修改该历史报告或回执使其通过。
- 新增网络恢复后的针对性测试：81 passed。最终完整回归：`.venv/bin/python -m pytest tests execution/tests -q`，**1,293 passed in 52.86s**。
- Agent 模型、runtime、指令、并发及相关配置读回一致；Autopilot 与触发器配置读回一致。
- 纳入保护核对的 575 个路径在结束时差异为 `[]`，覆盖生产 Paper 数据库、审批、输出、账本、账户路由及相关代码。临时任务注入的 `AGENTS.md` 已恢复。
- 结束时默认和桌面端 daemon 的 active/running task 均为 0。本轮未启动、停止或升级 daemon，外部观察到的版本变化不归因于本轮修改。
- 未执行生产 Paper 结算、Live、通知或正向交易审批；所有链结果保留 `execution_authorized=false`、`production_complete=false`。

当前可以确认多模型审阅机制可运行。生产定时入口与协调器的连接，以及一个未来真实合格交易日的完整验收，仍不在本次通过结论内。

使用说明：[多模型串行审阅器](MULTICA_REVIEW_CHAIN.md)。完整任务、模型、回执、恢复及保护证据：[审计 JSON](audits/2026-10-01-multica-chain-validation.json)。
