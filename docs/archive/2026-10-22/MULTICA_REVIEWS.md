# Multica 版本化审阅链

2026-09-22，Loop49建立审阅链，Loop50接入执行前关卡及控制台。本地契约与隔离恢复已验证，真实远端六角色协作尚未验收。

## 职责与边界

Research Lead 仍是唯一完整发布角色。新增的 `trading_team.multica_reviews` 对已发布且凭据合格的研究包做独立审阅，不调用模型、不创建远端任务、不修改计划、不批准或结算 Paper、不发送通知。

这是一条**发布后的非执行审阅链**，已成为新增正向审批与两条公开 LLM Paper 执行入口的强制前置条件，但不是发布前的独立 maker 流水线。审阅命令本身不执行交易；即使全部 `accepted`，`execution_authorized` 和 `production_complete` 仍为 false。既有七个本地 LLM 步骤不能因此称为七个远端 Agent。

顺序及必需检查：

| 角色标识 | Multica 角色 | 必需检查 |
| --- | --- | --- |
| data-quality | Data Quality | completed_session、source_integrity、coverage |
| market-analysts | Market Analysts | facts_supported、missing_data_disclosed |
| strategy-research | Strategy Research | counterevidence、claims_vs_evidence |
| paper-trader | Paper Trader | intent_constraints、account_context |
| risk-manager | Risk Manager | risk_derivation、portfolio_constraints |
| audit-verifier | Audit Verifier | version_chain、review_handoffs |

## 输入版本

准备前独立检查 v4 generation、角色与模型来源、行情归档、风险契约及发布账本确认。缺凭据的旧日报不能补签。输入包包含角色范围内的原文、文件哈希、生成回执、canonical plan_hash、数据日期、Project/Squad/独立 Agent 绑定和所有上游通过回执。

完整源文件集合另有 `files_hash`，因此不改变交易语义的计划备注变化也会使旧审阅失效。报告、角色绑定、上游回执或包内容变化同样不能沿用旧通过。准备和提交都重新核验来源；提交在远端身份核对后持共享锁再复核一次。

Data Quality 不拿后续分析正文作为自己的依据；其后逐级开放四分析报告、研究、提案及风险报告。包中的原文是待审数据，不是授予权限的指令。包只限定应用交接内容，不限制具备目录读写权限的进程；没有 OS 级沙箱或凭据隔离。

本地/manual 生成的来源身份保留为空对象，不冒充已核验远端来源。有远端生产者身份时，必须为配置的 Research Lead，并绑定相同 workspace；审阅不能复用生产者 task。不同审阅角色要求不同 Agent，且上下游 workspace 一致。

## 命令与提交

以下先使用隔离项目根目录；不要拿生产目录作为试验夹具：

```bash
.venv/bin/python -m trading_team.multica_reviews status --date YYYY-MM-DD --team <isolated-root>/trading_team
.venv/bin/python -m trading_team.multica_reviews prepare --date YYYY-MM-DD --role data-quality --team <isolated-root>/trading_team
```

`status` 只读，不初始化不存在的账本、不查询远端；全链 accepted 退出0，其余退出2。`prepare` 写不可变审阅包，返回 `bundle_hash`、`path`、`assignee_id` 和 `issue_title`，不会创建或派发 Issue。

取得授权后才能创建对应真实任务。须原样使用 prepare 返回的标题与角色负责人。2026-09-24 起的新任务标题为 `A股审阅 <中文角色名> <full-bundle-hash> <date>`，任务说明用中文写明只读范围、输入版本、检查项和禁止操作，命令及机器标识保持原值；2026-09-23 及更早的历史标题仍为 `A-share review <role> <full-bundle-hash> <date>`，不得批量改名或重签旧回执。该运行中的角色读取包并审阅后，提交结构化 JSON：

```json
{
  "schema": "multica_review_response.v1",
  "bundle_hash": "<prepare 返回的完整哈希>",
  "role": "data-quality",
  "verdict": "reject",
  "summary": "示例模板：实际审阅后替换所有结论和证据引用。",
  "checks": [
    {"name": "completed_session", "result": "unknown", "reason": "待核验", "evidence": ["<包内实际文件键>"]},
    {"name": "source_integrity", "result": "unknown", "reason": "待核验", "evidence": ["<包内实际文件键>"]},
    {"name": "coverage", "result": "unknown", "reason": "待核验", "evidence": ["<包内实际文件键>"]}
  ]
}
```

这是格式模板，不是可直接提交的审阅结果。每个必需检查恰好出现一次，必须有理由和包内文件引用。`accept` 要求全部 pass；出现 fail 或 unknown 必须 reject。多余审批字段、包外引用、漏项、重复检查或角色/哈希错误会被拒绝。

```bash
.venv/bin/python -m trading_team.multica_reviews submit --date YYYY-MM-DD --role data-quality --bundle-hash <hash> --response <review.json> --issue-id <actual-issue-id> --team <isolated-root>/trading_team
```

submit 使用现有 Multica CLI 只读核对真实 Issue、运行中 task、Agent、Project、workspace、日期及完整标题，然后写本地 `multica_review.<role>` 账本。daemon 环境必须来自实际任务，不能伪造环境变量、拿已终止任务补签或把 fixture 身份用于真实提交。

成功记录 reject 也会退出0、Run 状态也会 succeeded；这只表示**回执记录成功**。必须读 `receipt.response.verdict`、`is_latest` 及整个 `status`，不能把绿色 Run 当作审阅通过。运行中心的“执行前独立审阅”按日期显示六角色当前状态；回执详情展示记录时的结论、检查理由及证据引用，与当前有效性分开。

## 执行前强制关卡

`require_reviews(day, plan_hash, team=...)` 在共享状态锁保护下，由以下入口调用：

- `approvals.record`：新增 approved/reduced 之前。rejected 不需要审阅通过；既有相同 request_id 的重试只恢复已提交投影，不是重新批准。
- `run_paper_day`：版本与适用日期核对后、创建或写入账户之前，包含 dry-run。gate/no_gate 共用关卡，无闸门账户只豁免市场过滤。
- 旧 `execute.execute`：访问旧账户之前也要求当前审阅，已有批准不能绕过。此入口没有因此升级成完整双账户回执系统。
- `daily_finalize`：保留非执行反事实输出，随后以独立 review_gate 步骤检查，再执行 Paper。未完成交易会话或不请求 Paper 时跳过关卡，不宣称已经通过。

生成发布必须先于审阅，不能把本关卡移入发布前造成循环依赖。已发布研究可被审阅，即使后续 Paper 收尾被阻断。缺证不要求重新运行模型；补齐实际审阅后，可显式重试确定性收尾。不得自动派发、补造历史审阅或为过关重写计划。

关卡返回 `multica_review_gate.v1`，绑定日期、plan_hash、生成回执、完整源文件集合及六个角色回执哈希。双账户把该证明纳入 execution_inputs 及其哈希。Paper 重试和收尾缓存同时重新核对当前审阅与原成交回执；新审阅不能与旧结算拼成新通过。旧回执缺少该证明时保留历史，不自动补证或重新成交。

`GET /api/multica-reviews?date=YYYY-MM-DD` 只读。批准被关卡拒绝时接口返回409和 `review_gate_blocked`，尚未记录审批意图，不按通知/审批投影 unknown 处理。页面在审阅缺失、失效或读取失败时禁用批准，否决仍受原审批一致性条件约束。前端按钮不是安全边界，服务端每次重新核验。

本关卡当前会阻断整个新结算会话，包括该路径内的既有保护单评估；不是交易所托管保护或独立自动风控通道。旧账户历史审计、非执行反事实、只读估值与隔离撮合单元不是新订单授权。私有撮合函数不等于受审计公开入口。

## 失败与恢复

- 仅以最新角色运行作为当前状态；缺失、失败、拒绝、过期或损坏会阻断后续角色，不回退旧成功。
- 同一包和真实 task 的相同结果重试幂等；不同结果冲突，不能覆盖已记录结论。要重新审阅，使用新的合法任务并保留旧回执。
- 新拒绝优先于旧通过；旧成功重试返回 `is_latest=false`，不能重新打开后续链。
- 写步骤后、运行确认前崩溃仍显示 incomplete。确认原进程已停止后，真实身份仍有效的相同提交可续跑，保留失败/中断尝试；不能用已结束 task 伪造恢复。
- 回执、步骤确认、尝试记录或包文件缺失/损坏返回 invalid。不要直接改数据库状态、补造回执或删除旧运行来恢复绿灯。

完整工作流备份现在包含 `.runs/multica/reviews` 及已有账本。按 [WORKFLOW_RECOVERY.md](WORKFLOW_RECOVERY.md) 固定原 manifest 哈希、恢复至新目录，审阅状态应保持一致。恢复的远端身份只是历史证据，不是重新派发或执行的授权；账户专用备份不覆盖这条审阅链。

哈希是完整性检查，不是角色数字签名。检查字段与引用合法，也不证明理由正确、多个角色认知独立或策略盈利。只读 status 不重新查询远端，无法证明提交后的 task 最终成功或当前仍在运行。

## Loop49验收

- 34 项专项通过，全量 `tests execution/tests` 1104 项通过；缺实现、版本备注、并发快照、跨 workspace、生产者自审和丢失尝试等边界先复现后修复。
- 保留目录：`/private/tmp/quant-multica-reviews.Vkq5Lw/acceptance`。所有模型和远端身份为隔离夹具，不是真实模型/RPC。
- 测试外独立只读检查：六角色 accepted；新拒绝使下游 blocked；崩溃恢复保留两次尝试；备份恢复状态完全相同；缺少尝试记录的回执为 invalid。
- 生产9月22日只读查询为 `source_unverified`，退出2；没有对旧日报 prepare/submit。没有真实派发、通知、生产审批、Paper 结算或服务重载。

证据：[2026-09-22-multica-reviews.json](audits/2026-09-22-multica-reviews.json)。

## Loop50验收

- 新增20项集成测试，初始14项失败先复现；收尾缓存混用新旧审阅、审阅阻断误停反事实两项也先复现后修复。全量1124 passed in36.70s。
- 撮合/审批恢复等既有单元测试显式使用 `stub_research_reviews` 排除外部依赖；本文件对应的新集成测试使用真实本地审阅校验和隔离模拟远端身份，没有替换关卡。
- `/private/tmp/quant-review-gate.ut2Y8m/final-cases` 保留最终20项关卡及4项收尾场景。测试外只读核验双账户各一笔模拟成交、审阅证明一致；阻断账本保留 counterfactual succeeded / review_gate failed，账户未创建。
- 390/768/1100/1280px隔离页面无横向溢出；通过、失效、损坏、读取失败、批准/否决状态及HTML文本转义均验收。正式1910px页面与只读API显示今日source_unverified。
- 仅重载控制台，没有运行生产研究、提交审批、结算账户、发送通知或改远端任务。33个生产及历史验收保护文件不变。

证据：[2026-09-22-review-gate.json](audits/2026-09-22-review-gate.json)。真实身份与发布入口、远端交接、自然会话 Paper、异地恢复及独立前向效果仍未验收，整体目标未完成。
