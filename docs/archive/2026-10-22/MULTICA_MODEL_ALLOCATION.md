# Multica 角色模型分配

当前运行时更新（2026-10-22）：用户另行授权共享 daemon 重启后，项目独立 Codex 0.163.0 runtime `68bb6092-57e9-4a0a-a318-71c8680a654c` 已上线，仅 Research Lead 与 Audit Verifier 迁入，保留 `gpt-6.1-sol/xhigh` 与原进程参数。项目 bootstrap 仅同步 runtime_id；其他代理和全局 CLI 不变。未派探针或重跑历史失败，真实模型推理待未来原入口任务验证，不代表 M2/M3 通过。见 [重启与迁移审计](audits/2026-10-22-project-runtime-activation.json)。下文旧 runtime 不变等描述均为当时历史。

当前覆盖更新（2026-10-16）：用户明确授权后，仅将 Research Lead 和 Audit Verifier 从 `gpt-5.5` 切换为 `gpt-6.1-sol`，两者保留 `xhigh` 与进程参数 `-c model_reasoning_effort="xhigh"`，绑定 runtime 不变，已独立读回。未派启动探针或重跑历史失败；配置保存不证明模型推理成功，未来原入口自然任务仍须验收。下文10-01表格和调用结果保留为历史，不能当作当前模型配置。见 [迁移审计](audits/2026-10-16-authorized-codex-model-switch.json)。

日期：2026-10-01。用户要求利用已有 GPT、Claude、Kimi，为各角色分配模型，避免全部使用 GPT。

状态：远端配置已更新，七个角色逐项读回匹配。五个角色更换 runtime/model，两个 GPT 角色保留。

本次范围是现有七个 Multica Agent 的执行模型。`kimi_daily.py` 内部七个报告生成步骤继续使用原 Moonshot Kimi 配置；这两个层次不能混淆。

| Multica 角色 | 模型 | 分工理由 |
| --- | --- | --- |
| Research Lead / 研究负责人 | GPT-5.5（`gpt-5.5`） | 保留已验证的调度与发布入口，组织角色交接 |
| Data Quality / 数据质量 | Kimi K3（`kimi-code/k3-256k`） | 按现有检查项核对输入证据，数值、日期、覆盖率及哈希计算使用 Python |
| Market Analysts / 市场分析审阅 | Claude Sonnet 5（`claude-sonnet-5`） | 核查四类分析的事实和缺失披露，让 Kimi 生成的报告获得跨模型复核 |
| Strategy Research / 策略研究 | Claude Opus 5.5（`claude-opus-5-5`） | 集中处理论证、反证和证据与推断的区分 |
| Paper Trader / 模拟交易提案检查 | Kimi K3（`kimi-code/k3-256k`） | 核对提案与账户上下文，不承担撮合、计算或真实订单提交 |
| Risk Manager / 风险审阅 | Claude Opus 5.5（`claude-opus-5-5`） | 独立复核 Kimi 研究及提案中的风险论证，硬约束仍由代码执行 |
| Audit Verifier / 审计验证 | GPT-5.5（`gpt-5.5`） | 核对代码、版本哈希、审阅交接与执行证据 |

分布为 GPT 2 个、Claude 3 个、Kimi 2 个。这是职责与现有接入条件下的初始分工，不是模型能力排名，也没有证明收益或准确率提高。不同模型家族可以增加审阅视角，不能保证错误相互独立。

保留 Research Lead 的 runtime，使现有项目资源、唯一发布入口及 bootstrap 负责人绑定一致。其他角色使用同一台 MacBookPro、同一 daemon 下已登记在线的 Claude/Kimi runtime，不迁移设备。

固定 Claude 版本而不使用浮动别名：本机 Claude Code 为 2.1.283，官方文档说明 Sonnet 5.5 需要 2.1.284，故市场分析先用 Sonnet 5；Opus 5.5 所需的最低版本为 2.1.280。本次不升级全局 CLI。[Claude 模型配置](https://code.claude.com/docs/en/model-config)

Kimi 使用现有 Kimi Code 本地模型配置；GPT 使用既有 Codex runtime。不会将会员凭据改造成其他 API 认证，也不会新增计费账户。模型保存成功与账号实际可调用、余额充足是不同验收项。

只变更 Agent 的 `runtime_id` 和 `model`。保留角色 ID、指令、并发数、权限、思考配置、环境变量、MCP、历史任务、Autopilot 和交易状态。Multica 的新配置适用于后续运行，已经运行的任务不因此切换模型。[Multica 配置说明](https://multica.ai/docs/agents-create)

验证采用远端配置读回和未修改字段比对，不启动研究、审阅或交易任务，也不重写任何历史回执。旧运行继续属于当时的模型；后续真实运行应分别核对实际模型和结果，不能把本次配置验证视为业务闭环或模型效果验证。

最终复核：七个角色的模型、运行环境和未修改字段均匹配；原 Autopilot 返回配置摘要相同；纳入核对的九个文件（Paper 数据库、账户路由、bootstrap、workspace、生产 Runbook）哈希均未变。daemon 运行中，活动任务为零。没有可执行代码变更，因此未运行全量测试；没有模型推理请求，尚未验证新模型在本账户上的实际响应和角色任务表现。

审计及回退依据：`docs/audits/2026-10-01-multica-model-allocation.json`，记录每个角色原 runtime/model、目标及读回结果。回退仅恢复本次改动字段，不批量覆盖其他配置。

## 后续实际调用检查（2026-10-01）

用户随后要求检查七个角色能否正常工作，已逐个派发隔离的正反案例任务。Research Lead、Audit Verifier 的真实 GPT-5.5 执行及产物通过基础验收；两个 Kimi 角色均因本地登录文件格式损坏而认证失败；三个 Claude 角色均因后台连接已停机的 `127.0.0.1:3456` 转发端口而失败。整队暂不能正常协作，模型分配保持原样。

本补充覆盖了上文“尚未验证实际响应”的状态，但不代表完整业务链或模型效果验收。GPT 审计任务启动时仍出现连接重试。详情见 [实际运行检查](MULTICA_ROLE_HEALTH_2026-10-01.md) 与 `docs/audits/2026-10-01-multica-role-health.json`。

后续恢复：2026-10-01 16:15，Kimi 官方重新登录及原 K3-256k 模型调用已通过。角色分配保持原样；因 Multica 后台当时已停止，未将此结果扩展为两个远端角色验收通过。见 [Kimi 登录修复](KIMI_LOGIN_REPAIR_2026-10-01.md)。

当日晚间更新：Claude 三角色及 Kimi 两角色均已在原 Issue 上完成远端复测，加上上午通过的两个 GPT 角色，七角色基础任务全部通过。Kimi ACP 未返回用量模型标签，其模型证据仅包括配置绑定和真实 runtime 执行；不能虚构服务端模型确认。上述上午失败和下午待验收状态仅为历史记录。当前明细见 [实际运行检查](MULTICA_ROLE_HEALTH_2026-10-01.md)。
