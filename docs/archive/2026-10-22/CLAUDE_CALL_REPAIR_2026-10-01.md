# Claude 调用修复（2026-10-01）

当前状态：已完成官方 Pro 账号登录，Sonnet 5、Opus 5.5 真实调用成功；三个 Claude 角色均通过 Multica 基础任务复测。

## 原因与修改

`~/.zshrc` 和 `~/.bash_profile` 自动执行 `ccr activate`，并显式将 Claude 接口地址设为 `http://127.0.0.1:3456`。该端口已停止服务，导致此前 Multica 角色返回 `ECONNREFUSED`。现存 router 配置指向其他模型提供商，重启它不能证明正在使用 Claude。

已对两份 shell 配置作最小修改：移除自动启用 router 和旧代理密钥导出；仅当继承的接口地址等于上述旧地址时，清除对应的地址及认证环境变量。其他显式接口配置、网络代理、本地技能别名保持不变。原文件备份保存在 `~/.claude/repair-backup-20261001-163626/`，目录权限 0700、备份文件权限 0600；项目内不保存密钥。

## 登录与调用验证

- 修改前：新 zsh、bash 均带有旧 Claude 地址和认证环境变量。
- 修改后：两种 shell 均通过语法检查，旧代理变量均不再存在；合成的非旧代理地址和密钥仍被保留。
- 修复前官方认证为 `loggedIn=false`、`authMethod=none`，最小调用返回 `Not logged in` 且输入/输出 token 均为 0。失败探测已保留，未作为成功证据。
- 使用用户确认的账号完成官方登录后，认证为 `loggedIn=true`、`authMethod=claude.ai`、`apiProvider=firstParty`、`subscriptionType=pro`。未创建账号或购买订阅。
- `claude-sonnet-5` 返回 `CLAUDE_SONNET_OK 45`，退出码 0，`modelUsage` 确认实际模型；耗时约 5.63 秒。
- `claude-opus-5-5` 返回 `CLAUDE_OPUS_OK 45`，退出码 0，`modelUsage` 确认实际模型；耗时约 5.77 秒。
- 新 zsh、bash 使用日常 `claude auth status --json` 命令均读取到官方 Pro 登录，无需临时清理环境变量。

## Multica 三角色复测

复测前检查了全部 73 个 Issue，8 个未关闭且未阻塞的历史 Issue 均无活动执行。桌面后台在复测前已运行，进程未继承旧 Claude 地址或认证环境变量。每次只重跑一个原有合成测试 Issue，未启动历史业务任务。

| 角色 | 实际用量模型 | Issue | 结果 | 耗时 |
| --- | --- | --- | --- | --- |
| Market Analysts | `claude-sonnet-5` | SATU-68 | 通过 | 61 秒 |
| Strategy Research | `claude-opus-5-5` | SATU-71 | 通过 | 35 秒 |
| Risk Manager | `claude-opus-5-5` | SATU-72 | 通过 | 35 秒 |

三个执行均为 completed，Issue 均为 done；Agent/runtime 身份、实际模型用量、输入哈希、随机标识、正常/错误样例结论、计算值及结果文件均已独立核对。三个结果均保持 `execution_authorized=false`。上午的失败和取消记录保留。

- 市场分析：正确计算 2% 涨幅，拒绝无依据的盈利增长及资金流入断言。
- 策略研究：正确计算扣费后 -7 bps，拒绝未来数据与无独立样本的盈利宣称。
- 风控：正确计算买入后 25% 仓位，拒绝超限、市场门禁关闭和零流动性案例。

## 完整性与范围

模型分工和定时任务未变化，575 个受保护路径哈希均一致。结束时桌面后台运行且活动任务为 0，默认后台保持停止。

本次证明 Claude 接入、工具执行与合成案例的基础审阅能力，不代表完整生产研究、投资收益或实盘授权。此前 Kimi CLI 登录已修复，但两个 Kimi 角色尚未完成登录后的 Multica 复测；因此不能把整个七角色团队标为全部通过。

审计记录：[2026-10-01-claude-call-repair.json](/Users/henry/projects/quant/docs/audits/2026-10-01-claude-call-repair.json)。
