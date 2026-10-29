# 研究与 Paper 工作流证据恢复

## 范围

`workflow_backup.v1` 用于一起保存、核验和隔离恢复 LLM 研究与双 Paper 的业务证据。
它不是整个项目的系统镜像，不安装运行环境、不开启定时器、不派发 Multica、不发送通知。

纳入范围：

- 双 Paper SQLite 账户、工作流账本、`team.db`，包括已提交 WAL 中的数据。
- 研究上下文、计划、Live/Paper 决策、原始行情缓存、收盘归档和冻结日历。
- 七角色提示词、报告、结构化响应元数据和 `.input.txt` 输入。
- staging、归档、拒绝响应、逐次运行记录、通知 outbox、审批/准确率记录。
- 本地 Multica workspace/Runbook/bootstrap 映射、AGENT/Loop 审计文档。
- `docs/audits/` 下的非隐藏 JSON 审计证据，包括隔离 maker 产物与独立审阅回执；不纳入该目录的代码或环境文件。

不纳入 `.env`、凭据配置、代码/依赖、本机调度、远端 Multica 资源、已有备份、日志、锁文件和
旧策略账户。文件采用固定目录与后缀白名单；不将任意目录打包。角色输入与账户记录仍可能含
敏感业务信息，备份根目录权限为 `0700`，业务文件为 `0600`，不能把它当作公开材料。

缺少尚未建立的账户或 `team.db` 会在清单中明确记录，不初始化、不以零值填充；工作流账本
必须存在。坏数据库拒绝发布备份。历史缺证、失败和冲突按原样保留，不补签、不追认收益。

## 命令

从项目根目录运行。备份和恢复目标必须是**尚不存在且位于源项目之外**的目录。
以下路径仅用于演练，不是持久或异地备份位置；每次操作必须换新目录名。

```bash
.venv/bin/python -m trading_team.workflow_recovery backup \
  --target /private/tmp/quant-workflow-backup-NEW

.venv/bin/python -m trading_team.workflow_recovery verify \
  --source /private/tmp/quant-workflow-backup-NEW \
  --manifest-hash <backup返回的固定SHA256>

.venv/bin/python -m trading_team.workflow_recovery restore \
  --source /private/tmp/quant-workflow-backup-NEW \
  --manifest-hash <同一个固定SHA256> \
  --target /private/tmp/quant-workflow-restored-NEW
```

`backup` 支持显式 `--team /absolute/project/trading_team`。清单哈希应独立保管，不能在核验失败后
重算一个新哈希来“接受”变化。`manifest.json` 最后发布；仅存在目录不是备份成功。
恢复后验证全部文件与数据库，最后才发布 `restore.json`。错误返回非零退出码，部分目录保留，
不得覆盖重试。所有结果明确 `execution_authorized=false`、`execution_evidence=not_recertified`。

## 一致性与并发

先取得共享状态锁（首次使用也创建并锁定），再取得已知工作流进程锁和双账户写入锁；
锁被占用时立即返回 busy，不停止其他任务。审批、决策物化、发布/收尾、双 Paper 写入和
索引/评分/通知入口遵守同一共享锁；模型调用不持有这把锁，采集采用独立的逐日期锁。
SQLite 采用只读连接、读事务与 online backup，逐表核对 schema、列、行数与内容哈希；
普通文件在复制前后核对完整目录清单、内容哈希、文件身份和修改时间。所有纳入的数据库
还检查 `data_version` 和 inode，发现绕过应用锁的写入或文件替换则失败，不发布完成清单。

这是“协作入口互斥且快照窗口内源稳定，否则失败”的策略，不是跨文件/数据库事务。
后台写入频繁时需在安静窗口重试；不会自动暂停任务或接管另一运行。手工写入和未接入
共享锁的旧脚本仍需源稳定性检测，不能声称所有写入都具有事务保证。
未提交事务不进入快照。旧账本的 `running` 保持原值，但不是恢复机上实际存活进程的证明。

## 恢复后的验证

1. 先核对固定 manifest hash、全部文件及数据库内容，再查历史冲突和缺证。
2. 保留运行账本的原始 Run/attempt/step，包括 Multica 外部身份记录；这些记录不是重新授权。
3. 使用 `inspect_generation` 校验输入、角色产物、发布凭据与计划版本；没有凭据的旧日报仍是缺证。
4. 对合规隔离样本，重新收尾必须复用已结算凭据，账户不产生第二次成交；已发送 outbox 不重发。
5. 对未完成日会，复用角色必须继续满足原输入/提示词/模型/输出哈希，不因恢复而跳过校验。
6. 新版审批事件与 pending 状态一起恢复；先查 `approvals status`，再显式执行
   `approvals recover`。不得重新提交批准替代同步恢复；已要求的数据库/人工留痕目标缺失
   或模板损坏时保持阻止执行。恢复核对当前原计划与决策实际字段，不只比较哈希字符串。

源代码仅记录 `runtime_fingerprints`，没有随包安装；`verify` 证明包内容完整，不证明当前代码
与备份时一致或依赖可运行。激活前须独立比对运行时、安装依赖、核验数据及外部权限，并明确
授权新的触发。不可直接把恢复目录设为生产，或使用旧远端身份绕过当前 Multica 执行核验。

## 已完成验收

2026-09-22 05:33 上海时间，真实项目只读快照与隔离恢复：

- 694 个证据文件、4 个数据库、20 个工作流，所有尝试和步骤记录一致。
- 研究计划版本与原始收盘归档一致；恢复后 gate 0 冲突/22 缺证、no_gate 2 冲突/26 缺证。
- 另一个已结算的合成计划恢复后 `reused`，账户哈希不变，不重复成交。
- 离线单测验证完整生成无需模型重跑、中断生成仅续跑失败及下游角色、已发通知不重发。
- 新增恢复专项 37 项；全量 `754 passed`。生产账户、原计划、旧缓存和此前验收哈希不变。
- 无真实模型调用、通知、生产结算、远端派发或控制台服务变更。

证据：[恢复验收 JSON](/Users/henry/projects/quant/docs/audits/2026-09-22-workflow-recovery.json)。
演练目录 `/private/tmp/quant-workflow-recovery-CG62ZQ`。

后续并发验收见 [共享状态验收 JSON](/Users/henry/projects/quant/docs/audits/2026-09-22-state-concurrency.json)：
首次备份与共享写入互斥、跨进程 busy、异常退出释放以及已结算副本重试不重复成交。

Loop 39 新增审批真实进程中断演练：数据库提交后退出 91，pending 快照隔离恢复后完成同步，
仅保留一条事件/数据库记录/人工标记，重复恢复不改文件，双账户不变。详见
[审批恢复](/Users/henry/projects/quant/docs/APPROVAL_RECOVERY.md)。这不是跨存储原子事务。

Loop 40 新增不确定通知恢复：发送意图及全部尝试纳入快照，unknown 原样保留，恢复不会
自动重发。两个真实进程退出点与模拟送达验收通过；显式人工核验绑定原记录哈希和尝试。
详见 [通知恢复](/Users/henry/projects/quant/docs/NOTIFICATION_RECOVERY.md)。旧备份可能遗漏
备份之后的真实发送，激活前必须核对该差额，不能把缺少 outbox 当作未送达证明。

Loop 41 新增本机持久目录账、独立定时检查与只读恢复审阅，见
[持久备份](/Users/henry/projects/quant/docs/PERSISTENT_BACKUP.md)。目录账固定清单哈希，
未登记副本、部分目录及损坏历史均保留；本机配置不随恢复复制。完整性成功不是激活授权。

尚未完成：异地备份、外部失败告警、运行环境恢复与生产切换、真实通知端到端送达、
旧策略账户恢复及真实远端 Multica 角色 Run。不能把本轮演练称为已完成灾备或交易认证。
