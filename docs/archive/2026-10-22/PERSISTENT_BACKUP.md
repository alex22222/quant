# 持久备份与恢复检查

## 状态边界

这是研究与双 Paper **业务证据**的本机备份，不是系统镜像、异地灾备或执行授权。
备份沿用 `workflow_backup.v1` 白名单及共享写锁，详情见
[WORKFLOW_RECOVERY.md](/Users/henry/projects/quant/docs/WORKFLOW_RECOVERY.md)。

本机存储：`/Users/henry/Library/Application Support/HQCapital/workflow-backups`。
目录 `0700`、目录账和快照业务文件 `0600`。本机配置
`trading_team/.runs/backup-service.json` 绑定源绝对路径、存储目录和 store_id，**不随恢复包复制**。
复制配置到其他工作区不能继续向生产备份目录写入。

## 检查与调度

```bash
.venv/bin/python -m trading_team.backup_service status
.venv/bin/python -m trading_team.backup_launchd status
```

两项均只读；控制台 `GET /api/backup-service` 将备份事实与调度事实分开呈现。
`healthy` 仅表示最近快照完整、心跳新鲜且当前没有已知问题，不代表可运行、盈利或异地可恢复。
默认只重验**最近一个已登记快照**，历史快照不会每次全量扫描。已发现的旧损坏不会被新成功隐藏。

独立 LaunchAgent `com.hqcapital.workflow-backup`：

- 每个整刻钟检查，启动时也检查；最近成功快照达到 6 小时后创建新快照。
- 失败至少间隔 15 分钟重试；共享写入忙则退出，等待后续检查，不停止其他任务。
- 已配置目录总量上限 5 GiB、预留至少 1 GiB 可用空间，并在复制前估算快照空间。
- 容量不足时停止新增、保留已有内容，不自动轮换或删除旧备份。
- 30 分钟没有检查记录显示 stale。已加载、上次退出码、实际心跳和快照均须分别核对。
- Mac 关机、休眠或用户退出登录会影响运行；6 小时是计划间隔，不是承诺 RPO。

日志为 `trading_team/.runs/backup-service.log` 与 `backup-service.error.log`。
本轮未配置日志轮换和机器外告警；本机界面告警不能替代机器故障时的外部监控。

首次配置的显式命令如下；已有配置不会改绑，已有不同 LaunchAgent 不会覆盖或强行启用：

```bash
.venv/bin/python -m trading_team.backup_service init \
  --store '/Users/henry/Library/Application Support/HQCapital/workflow-backups'
.venv/bin/python -m trading_team.backup_launchd install
```

`tick` 为手工检查命令，不绕过 6 小时间隔，也不执行模型、通知、审批或订单。
`status`/`tick` 状态不健康返回非零；不因目录账丢失、损坏或不可读而自动重建历史。

## 中断与损坏

`catalog.json` 是备份尝试与完成登记的事实源。先持久化 running 尝试，再生成唯一新目录；
快照完整校验后才登记 succeeded 和固定 manifest hash。

若快照已生成、目录账尚未登记就崩溃，副本仍属未登记，不能从该副本自行计算哈希后追认。
下一轮取得共享锁后将旧 running 记为 interrupted，再使用全新目录。部分文件和旧快照都保留。
最新快照损坏会留下持续问题记录；新快照成功不能抹掉旧损坏。

固定哈希防止无意损坏被接受，但目录账不是身份签名，也不防止目录账与数据一起被恶意改写。
需要独立保存清单哈希、异地副本与外部告警。当前均不能宣称已完成，禁止重算旧哈希“修复”核验。

## 隔离恢复

从目录账取得已登记路径和原始 manifest hash，使用全新、项目外目录：

```bash
.venv/bin/python -m trading_team.workflow_recovery restore \
  --source '<已登记快照目录>' --manifest-hash '<固定SHA256>' \
  --target /private/tmp/quant-restored-NEW
.venv/bin/python -m trading_team.recovery_readiness \
  --restored /private/tmp/quant-restored-NEW --manifest-hash '<同一个固定SHA256>'
```

检查器只读验证完整清单与 restore 凭据，比较记录过的代码指纹，再检查数据新鲜度、研究发布、
风控契约、双账户历史、通知不确定状态和审批同步。代码匹配仅针对记录的文件，依赖安装、
外部服务和完整环境**没有验证**；项目没有 requirements.txt 时明确记录未包含依赖清单，
不能把缺少清单伪装成代码内容变化，也不能因此声称环境可复现。

`blocked` 返回 1；本地检查通过最多返回 `manual_review_required`，退出 0 也不是执行授权。
所有结果均 `execution_authorized=false`、`external_gap_reconciled=false`。
激活前必须核对备份之后的审批、订单和外部消息记录，确认代码/依赖、唯一写入者和调度，
取得明确授权。检查器不提供生产目录切换、不安装任务、不恢复发送、不派发 Multica。

## 本轮验收

- 39 项初始与边界用例通过后，真实演练发现依赖清单误报；先补失败测试，再修复。
- 专项最终 40 项；全量 `897 passed`，无真实模型、通知、生产审批/结算或远端派发。
- 持久快照 694 个文件、4 个数据库；隔离恢复后 gate 0 冲突/22 缺证，no_gate 2 冲突/26 缺证，与源一致。
- 子进程在登记前退出 94，旧副本不接纳；损坏副本保留，后续新快照仍显示旧问题。
- 首份快照早于检查器修复，检查结果正确保留该代码差异，不重写快照或原固定哈希。
- 07:30:05 自然定时触发后 runs=2、退出码0、心跳更新，未重复创建快照；正式 API 与 CLI 一致。
- 390/768/1100px 隔离视图与1280px正式视图无横向溢出；未配置、降级和健康状态均可区分。
- 初次误报演练保留于 `/private/tmp/quant-persistent-backup-hYTXoD`；复验使用新目录
  `/private/tmp/quant-persistent-backup-verified-ucscaE`，13 个生产保护文件哈希不变。

机器可读证据：[持久备份验收](/Users/henry/projects/quant/docs/audits/2026-09-22-persistent-backup.json)。
异地目标尚待指定；本地 Multica Runbook 更新不等于远端同步或真实角色 Run 成功。
