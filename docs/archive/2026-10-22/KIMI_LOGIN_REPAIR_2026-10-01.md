# Kimi 登录修复（2026-10-01）

结论：登录已恢复，原分配模型 `kimi-code/k3-256k` 的真实请求已成功。

发现两个连续故障：登录 JSON 末尾多了一个右花括号，解析失败；修复格式后，服务端仍拒绝旧刷新令牌，返回 `The provided authorization grant is invalid`，模型请求返回 401。

处理过程：在原私有目录保存 0600 权限备份，仅删除多余字符；随后通过官方 `kimi login --json` 完成重新授权，收到 `Logged in successfully`。新凭据可解析且未过期，文件权限保持 0600。官方登录自动更改的默认模型和思考偏好已恢复为原值：`kimi-code/k3-256k`、`default_thinking=false`。其他提供商配置未变。

2026-10-01 16:15:18（Asia/Shanghai）的实际请求退出码为 0，返回预期文本 `KIMI_LOGIN_OK 5`。请求使用现有 Kimi Code OAuth 配置、官方 `api.kimi.com` 和原模型，没有借用 Moonshot API Key 作为替代。

本次验证有两个边界：

- 恢复工作时，默认和桌面端 Multica 后台均已停止，因此官方重新登录成功后未重跑远端两个角色，不把登录恢复记为角色任务通过。
- 默认 Kimi CLI 启动受到现有“小红书 MCP”端点 `http://localhost:18060/mcp` 的 502 错误阻断。模型验证通过 `--mcp-config-file` 指定本次调用专用的空 MCP 文件完成，没有修改全局 MCP 配置。`--mcp-config` 只增加配置，不能替代默认文件。

在重新授权之前，Multica 的一次复测 task `01a0f5a2-7bab-7665-a933-3cc77e62a423` 虽标记 completed，但没有输出、工具调用或产物；底层实际为 401，因此该尝试未计为通过，Issue 保持 blocked，历史记录保留。

纳入对比的生产路径未变化，没有修改 Paper、审批、研究输出、角色模型分配或调度。

证据：[修复审计](/Users/henry/projects/quant/docs/audits/2026-10-01-kimi-login-repair.json)、[真实模型调用](/Users/henry/projects/quant/tmp/kimi-login-repair-20261001/direct-model-probe-clean.json)。私有备份位置记录于审计中，凭据内容未写入项目。
