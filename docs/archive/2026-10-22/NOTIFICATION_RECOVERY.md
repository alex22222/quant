# 通知发送与不确定送达恢复

## 协议

`notification_outbox.v2` 以渠道、业务日和 `plan_hash` 标识一次日报通知。所有发送入口
先取得共享写锁，再原子持久化 `sending` 尝试，之后才调用消息发送器。成功确认和尝试
历史一起落盘；发送前失败、响应不明、服务端确认与人工声明分别保存。

| 状态 | 含义 | 自动重试 |
| --- | --- | --- |
| `sending` / 对外 `unknown` | 已写请求意图，结果尚未可靠确认；也可能进程仍在发送 | 禁止 |
| `unknown` | 超时、异常、无有效回执或回执写入失败 | 禁止 |
| `retryable` | 发送前明确失败，或人工核验未送达 | 允许后续调用重试，不立即发送 |
| `sent` | 发送器/服务端确认，或人工声明已送达 | 跳过 |
| `legacy_sent` | 旧版成功回执 | 跳过，但不升级为新版证据 |
| `invalid` | 内容、身份、格式、尝试顺序或校验和冲突 | 禁止 |
| `missing` | 本地无记录 | 不代表历史从未发送 |

异常 `False` 不能证明未发送。`NotSent` 仅供适配器确认消息请求尚未发出时使用，例如
获取 token 失败。消息接口的业务错误、HTTP 错误、坏 JSON 或成功体缺少消息编号均按
不确定处理，不推断对端没有执行。即使“意图落盘后、实际请求前”退出，也宁可保留 unknown。

`push_feishu.push()` 和 `python -m trading_team.push_feishu` 统一经过 outbox，不再有普通
命令行绕过入口。发送前只读验证 generation、发布回执、原计划、决策版本及审批同步；
凭据延迟读取，不在模块导入时加载。卡片内容、接收者和应用身份绑定 fingerprint；未完成
通知重试时内容或接收者变化会拒绝，不偷偷改发。已发送的同一计划版本保持跳过，不因为
后来的审批文案变化再发一遍；此跳过也不声称更新后的文案已经送达。

飞书适配器保存 `message_id`，请求携带稳定 UUID。远端去重只作为附加保护；本协议不依赖
其窗口跨重启永远有效。官方接口与 SDK 说明参见
[发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create) 和
[UUID 字段说明](https://larksuite.github.io/oapi-sdk-java/com/lark/oapi/service/im/v1/model/CreateMessageReqBody.Builder.html#uuid-java.lang.String-)。
服务端消息回执不等于收件人已读；人工声明也不是服务端查询或身份签名。

## 核验与恢复

首先确认原进程状态，不以 `sending` 文件推断进程已退出；活跃发送会持有共享锁。
以下命令不会发消息。先在隔离副本操作，任何正式记录核验必须经过操作者确认。

```bash
.venv/bin/python -m trading_team.notification_outbox status \
  --team /absolute/restored-project/trading_team

.venv/bin/python -m trading_team.notification_outbox status \
  --team /absolute/restored-project/trading_team \
  --date YYYY-MM-DD --plan-hash <原计划哈希>

.venv/bin/python -m trading_team.notification_outbox resolve \
  --team /absolute/restored-project/trading_team \
  --date YYYY-MM-DD --plan-hash <原计划哈希> \
  --expected-hash <status返回的record_hash> --attempt-id <原attempt_id> \
  --outcome delivered --operator <核验人> \
  --evidence <可追溯的核验依据或记录位置> --message-id <已确认的消息编号>
```

只有确切的未送达证据才使用 `--outcome not_delivered`，此时不提供 `--message-id`。
单凭终端报错、没有本地回执、用户暂时没看到消息或等待超时，都不能证明未送达。
无法核实时保持 unknown，不删除记录、不换日期/计划哈希绕过。已有服务端消息编号的
尝试不能核验为未送达，也不能替换为另一个消息编号。

核验绑定原记录字节哈希及尝试编号，记录操作者、时间和证据；相同核验重复调用不改文件。
旧尝试或冲突声明被拒绝。`not_delivered` 只允许原有工作流在后续获授权运行时重试；命令
本身不发送。状态/核验返回 unknown、invalid、attention 或 retryable 时退出码为 1，需
查看 JSON；not_delivered 核验成功后仍为 retryable，因此退出码 1 不代表写入失败。

新尝试不会抹去原来的不确定状态和人工声明。研究工作流的推送步骤在 unknown 时失败，
重新运行复用合格的研究角色，但仍禁止重复发送；核验已送达后可跳过发送并完成工作流。
恢复旧快照可能缺少备份之后的发送，切换前必须核对备份外的送达证据，不能仅凭缺少记录
自动重发。任何外部程序绕过本入口、删除整个 outbox、错误人工核验，都不在保证范围内。

## 展示与存储

`GET /api/notifications` 和首页“通知与送达证据”为只读入口。历史 unknown/invalid 不因
最新计划成功而消失；展示计划、请求尝试、记录哈希、消息编号与人工核验，不显示凭据或
接收者明文。outbox 内保存内容/接收者 fingerprint，不保存 token、密钥和卡片全文。
人工 evidence 不得填写凭据。内容校验和用于一致性检查，不提供抗恶意重签能力。

统一 workflow 备份已包含 outbox，隔离恢复保持原状态和全部尝试，不发送、不自动核验。
参见 [工作流恢复](/Users/henry/projects/quant/docs/WORKFLOW_RECOVERY.md)。

## 验收

Loop 40，2026-09-22：起始相关基线 105 passed；首批 16 个新失败及后续边界先复现再修复。
通知/传输专项 35 项，全量 857 项通过；源代码检查无空白错误。

- 真实子进程在模拟送达后退出 92，恢复到新目录仍为 unknown；人工确认后跳过，模拟送达仅一次。
- 另一子进程在意图落盘后、发送前退出 93；核验明确未送达后才有第二个尝试，模拟发送仅一次。
- 695 文件/4 数据库快照保留不确定状态；双账户及 12 个正式保护文件哈希不变。
- 全链路离线测试：不确定通知两次运行只发一次，七角色只运行一次，核验后日会可以完成。
- 隔离界面 390/768/1100px 展示未确认、新确认、旧回执与历史尝试，无横向溢出。
- 无真实飞书请求、模型调用、生产审批/结算或远端 Multica 派发。

证据：[通知恢复验收 JSON](/Users/henry/projects/quant/docs/audits/2026-09-22-notification-recovery.json)。
实际飞书端到端送达、外部故障告警、持久/异地备份和人工激活仍需独立验收。
