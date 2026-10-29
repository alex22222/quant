# Multica 调度证据与展示

## 本轮范围

Loop46 为运行中心增加项目绑定的只读观察接口 `/api/multica-observation`。
连接、远端运行、本地研究关联、完成认证分别展示，不以 CLI 已安装、daemon 在线或资源存在替代业务成功。
没有新建、派发、重试或修改远端任务，没有切换运行时、定时计划或模型计费渠道。

## 证据边界

1. 读取本项目 bootstrap 的 Project、Autopilot、Agent、Runtime、Daemon、Trigger 编号；
   核对远端项目与负责人后才继续。绑定不一致立即停止后续项目查询。
2. 逐项比较 Runbook 原文哈希、Issue 日期模板、Autopilot 启用状态及唯一触发器的编号、时间和时区。
   漂移只显示，不自动修复或同步。默认期望来自 `multica/workspace.json`。
3. 查询最近五条 Autopilot 记录，按带时区的触发时间排序，保留最新失败。
   空历史、不可读取、损坏响应分别处理，不将未知解释为零次运行或成功。
4. 只展示负责人实际绑定且与 bootstrap 一致的 runtime，不借用另一台在线 runtime 的版本或能力。
   `online` 是远端登记状态，不是模型兼容性测试；默认模型没有返回具体值时保持未核验。
5. 只读查找本地 `daily_research` attempts；要求已核验的 Multica 来源、同项目、同 Issue，
   若远端有 Task 则还须同 Task。关联记录不是重新认证生成产物、审批、送达或 Paper 成交。
6. 接口 `completion_verified` 始终为 false。即使远端 succeeded 且本地关联 succeeded，
   也不在此接口签发端到端完成证明。`observed` 仅代表这些观察条件一致。

每条 CLI 读取设 8 秒超时；异常只返回错误类别，不返回 CLI 原始异常中的凭据。
远端 Agent 私有指令、环境配置及无关 runtime 不进入响应。失败说明限制长度并遮蔽常见
`sk-` / Bearer 格式，但这不是通用数据脱敏器；控制台仍只绑定 localhost。

运行中心将远端查询与本地账本加载分开；远端读取不阻塞本地列表/详情。
页面内远端查询防重入、最少间隔 60 秒，处于运行中心时周期刷新。本地列表仍每 30 秒刷新。
这不是独立监控服务、外部告警或远端历史归档。刷新失败显示未知，不复用旧成功作为当前状态。

## 2026-09-22 正式只读结果

11:00 上海时间，正式接口与页面一致：

| 检查项 | 事实 |
| --- | --- |
| Autopilot | `96f340ed-b1e8-44ca-9921-d8ff82d5d6c0`，active |
| 最近 Run | `01a0c691-0e7a-7b98-b915-52955a79f325`，failed |
| 触发 / 结束 | 08:43:10 / 08:45:09，来源 schedule |
| 失败原因 | 服务端要求更新 Codex 才能使用 gpt-6-astra |
| 负责人绑定 runtime | `b8f30287-d5e3-4a63-aaea-eb6dcad15fb7`，codex-cli 0.145.0，online |
| Runbook | 远端与本地内容不同 |
| 本地研究关联 | 未找到 |
| 完成认证 | 未认证 |

没有把更高版本桌面 runtime 的在线状态当作该项目已经升级。
旧 Kimi 直写日报仍未切换；调查及迁移顺序见 [RESEARCH_ENTRYPOINT.md](RESEARCH_ENTRYPOINT.md)。

## 原 Kimi 额度接入的检查

只读检查本机 Kimi CLI 1.49.0 后，没有直接增加 `kimi --print` 传输：

- CLI 当前选中 `kimi-code/k3-256k` / managed provider，存在 OAuth 配置；这不能证明与桌面相同额度。
- 已安装代码的 agent 加载器在内置工具后仍加载全局插件。空 `tools` / `allowed_tools` 并不等于无工具权限。
- print 模式自动批准工具调用，且默认还会读取全局 MCP 配置；不能把它作为已隔离的纯文本模型调用。
- 后续适配必须证明插件、MCP、Hook、工作目录及认证引用的隔离，同时保留逐角色输入/响应审计。
  本轮没有运行模型请求，没有复制令牌、修改全局配置或切换为 API 计费。

本机实现依据为安装目录 `kimi_cli/soul/agent.py`、`cli/__init__.py`、`app.py`；
print/自定义 agent 的公开定义见 [Kimi CLI 命令参考](https://moonshotai.github.io/kimi-cli/en/reference/kimi-command.html)
和 [自定义 Agent](https://moonshotai.github.io/kimi-cli/en/customization/agents.html)。

## 验证与未完成项

- 新增 20 项行为测试，首轮缺少模块全部失败，实现后通过；相关专项 47 passed。
- 主项目及执行层：`.venv/bin/python -m pytest -q tests execution/tests`，1033 passed in 22.67s。
- 真实失败响应、503 未知、远端成功但未认证、长错误文本与 HTML 转义均经浏览器检查。
- 12 秒远端延迟期间，本地列表和详情可用；390/768/1100/1280 像素宽度及正式桌面无页面横向溢出。
- 29 个保护文件哈希不变，包含正式账户、审批、今日原始产物和前序审计；无模型、通知、审批、生产结算或远端派发。
- 只重载 `com.hqcapital.console`；隔离展示服务已关闭。证据见 `docs/audits/2026-09-22-multica-observation.json`。

剩余工作仍是统一研究发布入口、取得已有所需授权后完成真实隔离 Run 验收、再有序停用旧直写任务。
原额度/API 选择、异地备份目的地与真实前向验证未完成。整体目标仍 active，不宣称日会恢复或建议盈利能力提高。
