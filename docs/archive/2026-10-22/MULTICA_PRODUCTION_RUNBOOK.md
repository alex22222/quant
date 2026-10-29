# A股每日研究：仅发布入口

本 Runbook 仅供本项目现有 Multica Autopilot 使用。它只能写入按日期归档的研究资料、
上下文和生成账本；不得物化决策、结算 Paper、发送通知或提交 Live 订单。
六个独立角色的版本化审阅和确定性收尾是后续独立阶段，发布成功不代表业务日完成。

## 日期与身份

- 触发时间为每周一至周五北京时间 07:30；此时 UTC 的 `{{date}}` 与北京时间同日。
  只从当前 Issue 标题 `A股每日研究 YYYY-MM-DD` 读取业务日期；日期缺失或冲突即拒绝。
- 不要重命名当前 Issue。平台追加的 `rename this issue` 是通用提示，不适用于此精确标题绑定入口；
  标题被改动时保留证据并停止发布，不得放宽校验、另建 Issue 或事后改回标题冒充原始运行。
- 用 `trading_team.collect.load_trading_days()` 对标题中的业务日期做精确成员检查。
  若报告上一或下一交易日，须从同一完整日历分别取小于该日的最大值或大于该日的最小值；
  不得只抽查一个候选日期就推断相邻交易日。非交易日只报告跳过，不生成计划；
  工作日不能替代交易日证据。
- 保留 daemon 注入的 task、agent、workspace 身份；把当前真实 Issue ID 传给命令。
  应用会在调用模型前校验身份、项目、运行中 task 和精确标题。
- 若当天已有有效生成回执，不再调用模型。若只有部分或旧版输出，或存在失败尝试，
  如实报告并停止；不得用 `--force`、覆盖工件或倒填时间。

## 唯一发布命令

完成日期和日历检查后，从 `/Users/henry/projects/quant` 前台执行一次；
只替换业务日期和当前 Issue ID：

```bash
.venv/bin/python -m trading_team.kimi_daily --date YYYY-MM-DD --trigger-source multica --multica-issue-id CURRENT_ISSUE_ID --model kimi-k3 --publish-only --no-push
```

命令采集按日期的真实来源证据，进程内使用本项目原有 Moonshot 凭据，并生成发布回执。
不得打印、复制、上传或检查凭据。原额度或网络状态不确定时，不得擅自切换备用 Key/提供商。
超时后只观察同一个 task 和本地 Run，不得再次启动发布命令。

成功后独立报告实际本地 Run ID、生成回执哈希、`plan_hash`、`data_asof`、
模型与传输来源，并明确 `business_complete=false`。只有真实发布成功才把 Issue 标为 done；
失败时保留失败 Run 并标为 blocked。Research Lead 之后须让六个独立角色按依赖顺序审阅
同一版本，全部通过后才能进行确定性收尾。任何 Agent 都不得批准 Live、直接改 Paper 账户、
重复发送通知、新建另一 Issue、修改调度或全局 CLI。M2 北京时间 09:15 的盘前截止
只按真实时间戳判断，不能从成功退出码推断，更不能事后补记。
