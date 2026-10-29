# 审批事件与同步恢复

## 事实与边界

`decisions/YYYY-MM-DD/decision.json` 是审批主事实源。新批复将 per-plan 状态与
`approval_event.v1` 事件链一起原子落盘，之后派生 `loops/approvals.json`、已有的 `team.db`
和可用的 `AGENT.md` 人工留痕。全部配置目标同步后才确认 `approval_projection.v1=complete`。
这不是跨 JSON/SQLite 的单一事务，而是主事实提交后可检测、可重复的派生恢复。

每个请求绑定日期、标的、动作、`plan_hash`、名称、备注和来源。`request_id` 在同一日期内
唯一；相同编号和内容只返回既有事件，不再次施加批复；相同编号不同内容返回冲突。
较晚的否决不能被旧批准请求覆盖。新计划保留历史事件，但清除当前批准，旧请求返回
`superseded=true`，不会重新批准新版本。恢复同时核对原计划、决策实际字段和事件链，
只复制正确哈希字符串不能授权已改动的价格、仓位或市场闸门。

事件哈希用于检测一致性，不是身份签名或防篡改存储。旧审批没有自动补写事件，不因
本次升级得到认证。恢复不产生新的审批意图、不下单、不结算账户、不派发远端任务。

## 状态与处理

| 状态 | 含义 | 操作 |
| --- | --- | --- |
| `complete` | 已记录事件与配置目标同步，当前来源校验通过 | 仍需执行闸门、撮合与账户校验 |
| `pending` | 主事件已存在，派生记录未齐或发生漂移 | 保留原事件，显式恢复同步；执行被阻止 |
| `invalid` | 当前计划版本、事件链或决策事实冲突 | 先调查来源，不重签、不强制改成功 |
| `legacy_untracked` | 没有新版审批事件 | 不是认证结论，保留旧有一致性检查 |
| `missing` | 无主决策记录 | 先检查研究和物化链路 |

已同步过的数据库或人工留痕文件丢失，必须先从可信备份恢复，不能降级为可选目标。
人工留痕模板损坏且无法恢复标记时仍保持 pending。首次没有 team.db 或没有可用人工模板
会明确记录 `absent` / `not_configured`，不会为了审批自动建立数据库或模板。

## 操作入口

从项目根目录运行；`--team` 指向待核验的 trading_team 目录。先在隔离恢复副本操作。

```bash
.venv/bin/python -m trading_team.approvals status 2026-09-21 \
  --team /absolute/restored-project/trading_team

.venv/bin/python -m trading_team.approvals recover 2026-09-21 \
  --team /absolute/restored-project/trading_team
```

`status` 只读；退出码 0 包括 `complete` 和 `legacy_untracked`，不得单凭退出码认定新版审计
通过。`recover` 取得共享写锁后恢复派生记录，不补造缺失主事件。失败保留已完成的工作；
按原事件继续恢复，不删除事件或换请求编号来绕过错误。

控制台入口：

- `GET /api/approvals?date=YYYY-MM-DD`：只读状态和事件历史。
- `POST /api/approve`：新批复，建议始终提供稳定 `request_id`。
- `POST /api/approvals/recover`，请求 `{"date":"YYYY-MM-DD"}`：仅恢复已记录批复的同步。

HTTP 409 `state_busy` 表示该次调用未开始写入，不能据此抹掉更早同编号调用的结果。
HTTP 503 `approval_projection_pending` 返回 `recorded_event_id`，表示事件已存在，不是
批复遭拒；应查询并恢复同步。HTTP 409 `approval_request_conflict` 必须调查编号内容冲突。
断线或 `approval_state_unknown` 时先查事件，不盲目创建新编号。浏览器当前页面会保留
未确认请求编号；刷新页面后没有跨页面持久化保证，必须先核验已有事件。

## 验收证据

Loop 39，2026-09-22：审批恢复专项 33 项、全量 820 项测试通过；故障用例先复现后修复。
真实子进程在隔离数据库提交后直接退出 91，pending 快照经统一备份/新目录恢复后完成同步。
事件、数据库记录、人工标记始终各一条；重复请求及恢复不改写投影文件，双 Paper 账户不变。
改变隔离原计划后旧请求不重新批准。另在隔离控制台验证失败提示、恢复按钮、带引号名称
及 390/768/1100px 布局，无生产批复、结算、模型调用、通知或远端派发。

证据：[崩溃恢复验收 JSON](/Users/henry/projects/quant/docs/audits/2026-09-22-approval-recovery.json)。
这些测试验证工程行为，不证明策略有正收益。通知不确定送达、持久/定时/异地备份与
人工激活、真实 Multica 角色 Run 仍须独立完成。
