# 研究入口调查与迁移边界

## 2026-09-22 已查实的故障

问题不是“风险闸门过严”。当前存在两条不同的日会入口，只有一条进入新研究执行器。

| 入口 | 本次事实 | 业务结果 |
| --- | --- | --- |
| Kimi Blueprint 旧任务 | 08:43:24 启动，外部状态 succeeded | 模型直接写七角色报告、日报及 plan.json；没有生成回执和 daily_research 账本 |
| Multica Autopilot | 08:43:10 触发，08:45:09 failed | gpt-6-astra 被运行时版本要求拒绝，未形成已验证角色执行 |
| 09:15 Kimi API 兜底 | 原先只检查 daily_conclusion.md 是否存在 | 会把旧入口缺证产物直接跳过，掩盖入口迁移未完成 |

以上时间为上海时间。本轮未主动触发模型、消息或远端任务。

### 旧入口的直接证据

Blueprint 自动化编号 `automation_7a27faee-d643-4f35-a89b-daa3798b2452`，本次运行
`run_d99e2a5d-6a3d-47c4-aced-315f3be0b598`。其现行提示词仍要求模型直接写
`outputs/<date>/01~07`、`daily_conclusion.md` 和旧格式 `plan.json`。

该运行日志关联的 Kimi 会话 turn 10 保存了九次直接写入，以及 step 36 的
`daily_finalize.materialize_day('2026-09-22')` 调用。它解释了上轮在 09:00:26
发现的 team.db 新增决策/计划记录，不是保护单演练写入，也不是新的审批或成交。

当前计划版本：

```text
eea77c2b61976fa04ea015db80732bb6b751f49140ea0ffff0f91c0b1589c830
```

该计划缺 `risk_contract`，输出目录缺 `generation.json`，运行账本没有当日
`daily_research`。两个 Paper 组合仍为 `pending`。Kimi 自身记录飞书因生成凭据缺失
而被拒绝，不应回退推送校验，也不能根据 Blueprint 的 succeeded 推断研究闭环成功。

### Multica 的独立故障

只读查询已配置 Autopilot 的真实运行历史得到：

- Autopilot：`96f340ed-b1e8-44ca-9921-d8ff82d5d6c0`。
- Run：`01a0c691-0e7a-7b98-b915-52955a79f325`，来源 schedule，状态 failed。
- Issue：`01a0c691-0e84-7cb3-a9cd-7a3fdb640938`；返回的 task_id 为空。
- 服务端报错要求使用更新的 Codex 才能运行 `gpt-6-astra`。
- 本项目绑定的 runtime `b8f30287-d5e3-4a63-aaea-eb6dcad15fb7` 登记为
  `codex-cli 0.145.0`。另一桌面 runtime 在线并登记 `0.155.0-alpha.9.2`，但没有将其
  切换给本项目，也没有据此宣称兼容性已验收。

这更新了早期“尚无自然 Run”的观察，但不是一次成功的角色 Run。
CLI 的安装和升级应按 [OpenAI 官方说明](https://learn.chatgpt.com/docs/codex/cli)
核对实际启动的二进制；仅安装新版本并不能证明 daemon 已使用它。

## 本轮修复

`kimi_fallback` 不再以报告文件存在作为完成条件：

1. 主任务仍在 running 时只等待，不接管。
2. 只要当日产物目录非空，就只读校验归档输入、角色产物、结构化风控、计划哈希和生成凭据。
3. 发布步骤须已确认；同日最新 daily_research 必须成功且绑定该计划。旧成功不能掩盖后续失败。
4. 缺证、损坏或收尾未完成返回 blocked，保留原件，禁止自动重生成、补签和发送。
5. 无产物且原定时运行明确因额度耗尽失败，才允许既有 API 兜底；运行来源记录原 Kimi runId。
6. 主任务成功却无产物、非额度失败均返回 blocked，不擅自换模型或计费通道。

诊断命令只读，不加载 API 密钥、不联网、不创建账本、不恢复状态：

```bash
.venv/bin/python -m trading_team.kimi_fallback --date 2026-09-22 --check
```

退出码：0 表示本次无须兜底，包括正在运行或没有主任务，**不是日会整体健康证明**；
1 表示 blocked；2 表示已确认需要额度兜底，但 `--check` 未实际调用。当前实际结果为
`blocked / existing_generation_unverified`。

有效生成被跳过也不重新验证全部决策、消息及账户状态，更不是成交或送达认证。完整运行状态
仍需分别查看运行中心、审批/通知证据和 Paper 审计。此修复无法阻止一个仍持有文件写权限的
旧 Agent 继续直接写入，真正入口切换尚未完成。

## 切换顺序与验收

优先保留既有“原额度优先、耗尽才使用 API”的约束；已向用户询问是否改为统一 API，未获回答前
不改变计费策略。可以选用不同模型传输，但不能存在两套独立的计划发布器。

1. 核对并修复 Multica 实际使用的 Codex 二进制/兼容性，保留本次失败 Run，禁止伪造成功记录。
2. 选择模型传输：保留桌面额度需要逐角色输入/响应适配，不能事后把直接写入的旧报告补成认证产物；
   统一 API 则复用当前 DailyMeetingRunner，明确费用来源。两种方式都必须经过同一确定性发布流程。
3. 经明确授权后，以隔离目录执行一次真实集成验收，再同步远端 Runbook。不得通过修改自动触发器
   绕过先前受阻的派发授权。
4. 验证每一角色的输入/输出哈希、失败续跑、结构化风控、计划审批版本及消息不确定状态。
5. 确认新入口可用且没有运行中写入者，再停用旧直写入口；只保留一个研究调度责任方。
6. 下一自然交易日验证真实完整链路。旧日报、失败 Run、未成交和缺证仍原样保留。

本轮没有更换运行时、停用任务、同步远端 Runbook或重跑今日研究；不将“诊断修复”宣称为
“日会恢复”。也未自动放松风险、启用 Live 或结算生产 Paper。

## 验证证据

- 修改前相关测试：81 passed；新增回归首次为 17 failed / 2 passed，修复后相关 59 passed。
- 主项目与执行层：`.venv/bin/python -m pytest -q tests execution/tests`，1013 passed。
- 根目录裸 `pytest -q` 会收集 vendored rqalpha，出现 31 个导入/依赖错误，包括 cli/talib；
  不计为主项目通过，也不为此修改外部代码或安装无关依赖。
- 实际只读检查阻断旧日报，网络和凭据读取在验收脚本中被禁止；28 个受保护文件哈希不变，
  包含两个 Paper 数据库、team.db、审批及今日原始输出/决策。
- 证据：`docs/audits/2026-09-22-research-entrypoint.json`；验收脚本与本地过程记录：
  `/private/tmp/quant-entrypoint-63uop_le/`。

整体目标仍未完成。下一轮聚焦实际运行时与研究入口统一，而不是继续增加交易功能。
