# KIMI_HANDOFF.md — Kimi → Codex 项目交接文档

> 生成时间：2026-08-23 · 生成者：Kimi Work
> 项目路径：`/Users/henry/projects/quant` · GitHub：`github.com/alex22222/quant`（main）
> 项目代号：**HQCapital** —— 参考 TradingAgents 多智能体 LLM 金融交易框架构建的虚拟投资公司，目标是 A 股个人量化闭环（数据 → 策略 → 回测门禁 → Paper 模拟 → 复盘）。
>
> 本文档由 12 个 Kimi 会话（2026-08-11 ~ 08-23）的 wire.jsonl 提取整理而成。
> 完整对话提取稿见 `docs/handoff_extract/`（每会话一个 .md + `_SUMMARY.md` 汇总）。
> 权威运行日志见 `AGENT.md`（Loop Engineering 迭代日志，Loop #1~#17），治理文档见 `docs/`。

---

## 1. 系统架构

### 1.1 总体分层

```
                        👤 老板（PM，最终审批权）
        ┌───────────────────────────┴───────────────────────────┐
  第一层：LLM 交易团队（trading_team/）          第二层：确定性流水线（pipeline/ + strategies/ + paper/）
  分析师×4 → 多空研究员 → 交易员 → 风控经理        data → strategy → backtest(门禁) → paper → review
  产出：日报提案（只有提案权，无执行权）            产出：status.json 事实源 + paper.db 台账
        └───────────────────────────┬───────────────────────────┘
                        第三层：实盘执行（execution/，建设中）
                        signals → market → order_manager → risk_guard → broker(mock/easytrader/xtquant)
        控制台 console/（只读展示 + 审批写接口）· 情报线 quant-x-monitor/（X 量化博主监控→飞书）
```

### 1.2 目录结构与关键文件

| 目录/文件 | 职责 |
|---|---|
| `pipeline/` | 五阶段流水线：`run.py`（入口）、`stage_data/strategy/backtest/paper/review/audit.py`、`gate.py`（门禁 v2）、`sweep.py`（参数扫描）、`promote.py`（原子晋级）、`universe.py`（point-in-time 股票池）、`paper_dryrun.py`（预演） |
| `strategies/` | 策略库：`base.py`（统一信号接口 StrategyData 协议 + compute_targets 纯函数）+ 各策略文件。回测与 Paper 共用同一信号核心 |
| `paper/` | Paper 台账：`paper.db`（SQLite）、`execution_model.py`（T+1 撮合：费用/涨跌停/整手/顺延重试） |
| `trading_team/` | LLM 七角色团队：`prompts/01~07`（角色章程）、`collect.py`（A股采集）、`collect_global.py`（全球宏观雷达）、`briefing.py`、`schemas.py`（结构化结论块）、`consistency_check.py`（三源校验）、`approvals.py`（批复台账）、`execute.py`（提案→Paper 执行器）、`auto_execute.py`（默认冻结）、`db.py`（team.db）、`push_feishu.py`、`conclusion_block.py`、`patch_bundle_from_context.py`（bundle 补丁） |
| `execution/` | 实盘执行层（Loop #14）：`broker_base/mock/easytrader.py`、`order_manager.py`、`risk_guard.py`、`runner.py`、`ledger.py`（execution.db）、`notifier.py`、`tests/test_guard.py`。**尚未上实盘** |
| `console/` | 控制台：`index.html`（单页应用，侧边栏菜单）+ `serve.py`（静态服务 + /api/approve、/api/team-db）+ `dev-server.py`（文件变更自动重载）+ `news/`（新闻存档）+ logo/favicon |
| `quant-x-monitor/` | 情报线：每日 21:43 监控 9 个量化 X 博主 → 飞书推送 + `reports/` 存档 |
| `kde_levels/` | KDE 支撑位算法研究（已闭环：无 edge，见 §3） |
| `rqalpha/` | RQAlpha v6.3.0 源码（editable 安装），A 股回测引擎 |
| `status.json` | **机器写的事实源**（策略注册表 + 各阶段快照） |
| `AGENT.md` | **人机共写**：运作协议、角色职责、Loop 迭代日志 |
| `docs/` | GOAL.md / PRD.md / ARCHITECTURE.md / WHITEPAPER.md / PROJECT_REVIEW.md / IMPROVEMENT_PLAN.md / STRATEGY_LIBRARY_REVIEW_A_SHARE.md / execution.md / improvement-suggestions-quant-wiki.md / ELI5-系统说明书.html |
| `tests/` | 134 项 pytest（基础设施 + 策略核心 + 撮合 + 门禁 + 股票池） |
| `reviews/` | 每次 pipeline.run 的复盘报告 |
| `reports/` | 回测产物（gitignore，本地）：`gate_v2/`（门禁成绩单）、`sweep/`（参数扫描） |

### 1.3 角色职责（HQCapital 组织架构，源自 TradingAgents 原文）

- **分析师团队 ×4**：基本面（陈致远）/ 情绪（林晓晴）/ 新闻（周文轩）/ 技术（赵铭远）—— 只输出本维度研究报告
- **研究团队**：多头研究员（吴思远）/ 空头研究员（孙若兰）—— 只组织多空辩论 + 信心度评分
- **交易员（郑远航）**：只把研究结论转为计划表（方向/仓位/入场/止损/失效条件）
- **风险经理（黄守正）**：只审敞口、行使降仓/否决权
- **老板（用户本人）= PM**：唯一审批权

**铁律**：角色边界不可逾越；LLM 只有提案权（pm_status=pending），不碰 paper.db、status.json、策略注册表。

---

## 2. 策略决策史（关键结论，勿走回头路）

### 2.1 当前注册表状态（2026-08-23 23:30 核实）

| 状态 | 策略 |
|---|---|
| **live：无** | （门禁 T+1 重跑后 8 策略全拒，全部回炉） |
| candidate | momentum_rotation、ma_trend_bluechip、sharpe_momentum、turtle_bluechip、week52_momentum |
| retired | demo_dual_ma、momentum_stops、rotation_300_500、rsi2_reversal、reversal-during-earnings-announcements（美股策略已移出） |
| research | base |

### 2.2 已验证的研究结论（有数据支撑，不要重复证伪）

1. **KDE 支撑位（_zheergen 推文）**：算法工艺真实可复现，但 63,841 样本事件研究证伪触位预测力（p>0.18）；作止损锚全程版压回撤（28.2%→19.8%）但夏普受损（0.458→0.371）、换手 +73%；盈利仓版两头最差（夏普 0.145）。**裁决：不如不止损或 ATR 吊灯**。代码在 `kde_levels/`。
2. **momentum_stops / momentum_rotation**（同族同参）：旧门禁 pass（年化 10.4%/回撤 28.2%/夏普 0.46），门禁 v2 + T+1 口径下 reject（OOS 超额 -4.2%、最长回撤 1735 天、参数扰动全崩）。已退役/降 candidate。
3. **turtle_bluechip E18 晋级被推翻**：STRATEGY_LIBRARY_REVIEW_A_SHARE.md 审查认定 E18 方法论不成立——调参数据=验证数据（2024-2026 被反复用于 E3-E18 实验）、晋级提交同时放宽门禁（扰动回撤 ×1.2 容差带）、回测信号当日 15:00 成交 vs Paper T+1 开盘的口径分裂。改为 T+1 成交后超额 +0.5%→**-0.7%**。turtle 已降回 candidate，E18 参数冻结。
4. **rotation_300_500 二八轮动**：调参救不回（结构问题），加债券/红利 ETF 避险腿的改良未做，已 retired。
5. **rsi2_reversal / sharpe_momentum / ma_trend / week52**：门禁 v2 全拒（高换手在成本模型下原形毕露 / 收益不达标）。

### 2.3 治理纪律（已制度化，必须遵守）

- **门禁规则与策略晋级分离提交**：任何门禁改动必须独立 commit + 写明动机 + 全策略重跑；**永远不许和策略晋级出现在同一提交**（E18 教训）。
- **原子晋级**：状态变更只走 `pipeline/promote.py`（记录策略 hash/参数 hash/数据版本/门禁版本/批准人）；`stage_audit` 自检环路会自动降级脏编辑。
- **数据分区正名**：2024-2026 叫 validation，不叫 OOS；真正的 test set 是未来的数据。
- **多重试验修正**：简化 Deflated Sharpe 已入门禁 + 实验台账落 git（试得越多门槛越高）。
- **防工业化 HARKing**：入 candidate 必须 `pipeline.promote --differentiation` 写明与在库策略的区分度（fail closed）。
- **turtle 再晋级条件**：纯前向 6-12 个月 Paper + T+1 口径下正超额（时间等待，不是工程量）。
- **成交口径统一**：全部日线策略 T 日信号 → T+1 开盘成交（回测与 Paper 同口径）；下单代码只允许出现在 open_auction（有 AST 守卫测试）。

---

## 3. 已完成工作（按时间线）

| 日期 | 事项 | 证据 |
|---|---|---|
| 08-11 | GitHub 量化项目调研（A股/港股/美股分层名单 + 8-12 周研究路径） | conv-d87a |
| 08-15 | X 博主 @wepoets1107 可靠性审计（代码安全✅/回测不可信⚠️）；量化博主分层名单；**quant-x-monitor 上线**（9 博主每日 21:43 飞书推送） | conv-54d1 |
| 08-16 | **RQAlpha 环境落地**（.venv Python 3.12 + 米筐 bundle）；双均线验证回测；动量轮动回测（年化 10.4%）；KDE 三层验证链闭环 | conv-36a5 |
| 08-17 | CCXT 4.5.73 安装 + OKX 连通（代理 127.0.0.1:7890；Binance 451 被墙）；`okx_client.py`；首次推送 GitHub（发现并隔离飞书 secret 到 .gitignore） | 同上 |
| 08-22 | **闭环 v1**：GOAL/PRD/ARCHITECTURE + 五阶段 pipeline + 控制台 + Paper（Loop #1-3）；月度 Loop 定时任务（每月 2 号 21:07）+ 飞书复盘推送；控制台菜单化重构 + 组织架构页；新闻页（日报回填 68 条）；**trading_team 七角色首跑**（Loop #5，平安 15% 提案）；晨会 Automation + briefing.py（Loop #6）；飞书日报推送 + 老板审批约定（Loop #8）；**HQCapital 品牌 + 角色职责制度化**（Loop #9）；审批面板 + approvals.json（Loop #10）；**两层打通 execute.py + team.db + auto_execute 每 5 分钟轮询**（Loop #11）；观察池扩至 11 只 + 标的池页；**全球宏观雷达** collect_global.py（Loop #12）；mac-a股量化接口调研（QMT+PTrade+桥接结论） | 多会话 |
| 08-23 | **策略库扩容**（6 新策略 + 策略库页 + 门禁成绩单，Loop #12）；sweep.py 参数扫描（turtle 12 组 / rotation 5 组，Loop #13）；**execution/ 实盘执行层建成**（mock 两日演练全绿 + 风控 10 用例，Loop #14）；**PROJECT_REVIEW 全量整改**（事实源统一/Paper 真跑 live/T+1 撮合/门禁 v2/LLM 三层降级/98→134 测试）；turtle E18 攻关→晋级→被审查推翻→降级冻结；**门禁 T+1 重跑 8 策略全拒**；point-in-time 股票池构建器 + 81 期快照；策略库精简（3 退役）；quant-wiki 四文改进落地（结构化结论块契约/舆情温度/防 HARKing/LLM 护栏成文）；角色拟人化命名；HQCapital logo/favicon；白皮书 + ELI5 图解说明书；dev-server 常驻自动重载 | 多会话 |

### 定时任务（Blueprint Automations，当前在线）

| 任务 | 触发 | 说明 |
|---|---|---|
| 量化闭环·月度 Loop | 每月 2 号 21:07（cron `7 21 2 * *`） | 更新数据包 + 五阶段全流程 + 复盘 + 飞书推送 |
| 交易团队日报 | 每天 08:43（Asia/Shanghai） | 双采集 → 七角色 → plan.json → accuracy → 飞书推送；非交易日自动周报复盘模式；幂等轻量模式 |
| 量化博主 X 监控 | 每天 21:43（cron `43 21 * * *`） | 9 博主新帖 → 飞书 + reports/ 存档 |
| auto_execute | 每 5 分钟 interval | approved 决策自动执行到 Paper——**当前默认冻结**，需 `QUANT_AUTO_EXECUTE=1` 解锁 |

### 推送历史（GitHub main）

最新提交 `fdf3259`。关键节点：`fc1bd75`（首次）→ `e475551`（闭环 v1）→ `6d8230d`（定时任务）→ `2eacb1b`（team.db）→ `883daaf`/`49397a3`（整改+execution）→ `4e16f02`（turtle E18，后被推翻）→ `c959297`（T+1 口径）→ `2a0dbd1`（执行记录三）→ `ea9c0b8`/`f94e2e0`/`fdf3259`（quant-wiki 建议落地）。

---

## 4. 已知问题（未修 / 需关注）

1. **forex（USDCNH）源不稳**：东财 push2 夜间代理抖动，collect_global 按「允许缺失」降级；持续抖动需换主源。
2. **市场宽度统计 filter 未生效**（Loop #5 遗留数据缺口，至今未修）。
3. **东财 F10/push2 接口偶发 proxy 重置**：重跑自愈，但缺自动重试机制（Loop #7 下一步②未完成）。
4. **rqalpha bundle 数据补丁**：官方包只到 7/31，靠 `patch_bundle_from_context.py` 补 8 月数据；宁德 8 月分红除权缺口（≤0.36%）未修，等下月官方包整体替换；指数 total_turnover 补 0。备份在 `~/.rqalpha/bundle-bak-20260731`。
5. **AGENT.md Loop 编号有重复/乱序**：历史上有两个 Loop #11、两个 Loop #12、两个 Loop #14、一个 Loop #6 重复——并行会话写入所致，读日志时以内容为准。
6. **status.json 内部快照可能滞后**：注册表手工编辑后 stages 快照曾不自洽（stage_audit 现已兜底，但历史快照未全部刷新）。
7. **auto_execute 冻结**：每 5 分钟轮询仍在跑但默认不执行（安全设计，非 bug）。
8. **并发行文风险**：多个 Kimi 会话同时改 console/index.html、stage_paper.py 曾互相覆盖（审批面板 JS 被写断、Loop 编号撞车）。Codex 接手后建议单线程操作或先 git pull。
9. **execution/ 与 paper/ 是两套执行栈**（Loop #14 备注需人工对齐关系）。
10. **晨会成本**：一场完整晨会 ≈ 100 万 token 吞吐（满价约 12 万），日报 Automation 全量跑一次 ≈ 344 万吞吐；降本手段已分析（关 include_content、合并搜索）未实施。

---

## 5. 未完成事项（按优先级）

### P0 — 等老板决策 / 时间等待

- **turtle_bluechip 前向验证**：E18 冻结中，需 6-12 个月纯前向 Paper 记录（T+1 口径正超额）才可再晋级。备选手段已备好：chandelier_atr 吊灯止盈、ma_slope_days 斜率过滤、max_hold_days 时间止损。
- **8-28 起 accuracy.json 首批 T+5 到期**：跑首次准确率回填，验证角色评级质量。
- **每天 08:43 日报的提案批复**：批复入口 = 控制台审批面板 / Kimi 对话 / 编辑文件，三处写同一台账。

### P1 — 工程量小、价值明确

- **实盘链路**：Windows PC 验证「平安证券是否支持同花顺通用下单」（docs/execution.md 验证清单）；不支持则国金佣金宝开 miniQMT（近 20 日日均 ≥10 万验资），届时写 `broker_xtquant.py`（Broker 接口不变，~200 行）。
- **Intel Mac 家庭交易中枢**：VMware Fusion 13.6.4（免登录直链见 conv-668b 摘要）+ Win11 x64（无需激活）+ broker_http.py/broker_server.py 桥接（未写）+ Tailscale 组网。主力 M Mac 只做开发/观察。
- **收益归因输出**：每笔交易分解为信号收益/开盘跳空/费用/滑点/未成交损失。
- **晨报 token 降本**：搜索关 include_content、情绪/新闻检索合并。

### P2 — 中期

- **股票池二期**：历史时点成分股快照接入策略（tushare/akshare 指数权重历史，独立数据工程）。
- **撮合深化**：封单量/部分成交模型。
- **量化新闻 → 策略候选流水线**（Loop #4 挂起项）：高含量日研究线索沉淀为 research。
- **晨会前向验证月报自动化**（自动 audit + Paper 记账 + 月报，提议过未建）。

---

## 6. 关键命令速查

```bash
cd /Users/henry/projects/quant

# ===== 流水线 =====
.venv/bin/python -m pipeline.run                      # 五阶段全流程（一个 Loop）
.venv/bin/python -m pipeline.run --stages data,paper  # 子集
.venv/bin/python -m pipeline.run --update-data        # 先更新 bundle 再跑
.venv/bin/python pipeline/paper_dryrun.py             # paper 零风险预演
.venv/bin/python scripts/run_gate_v2.py               # 门禁 v2 全量重跑
.venv/bin/python pipeline/sweep.py ...                # 参数网格扫描
.venv/bin/python -m pipeline.promote ...              # 原子晋级（唯一合法入口）

# ===== 测试 =====
.venv/bin/python -m pytest tests/ -q                  # 134 项

# ===== 回测（rqalpha）=====
.venv/bin/rqalpha run -f strategies/xxx.py -s 2020-01-01 -e 2026-08-01 \
  --account stock 100000 --report reports/xxx --plot-save reports/xxx/equity.png

# ===== LLM 交易团队 =====
.venv/bin/python -m trading_team.collect              # A股采集（东财主+腾讯兜底）
.venv/bin/python -m trading_team.collect_global       # 全球宏观雷达
.venv/bin/python trading_team/briefing.py             # 晨会数据包
.venv/bin/python -m trading_team.build_index          # 重建 outputs 索引
.venv/bin/python trading_team/execute.py YYYY-MM-DD [--confirm]  # 提案→Paper（默认 dry-run）
.venv/bin/python -m trading_team.push_feishu          # 飞书推送日报卡片

# ===== 控制台 =====
.venv/bin/python console/dev-server.py                # 常驻开发服务器（自动重载）→ http://127.0.0.1:7100/console/
# 停止: kill $(lsof -ti:7100)

# ===== 实盘执行（dry-run 默认）=====
.venv/bin/python -m execution.runner --mode dry-run   # mock 通道演练
.venv/bin/python -m execution.runner --mode live      # 需 config.json live_enabled=true（双开关）

# ===== 情报线 =====
cd quant-x-monitor && ../.venv/bin/python run_monitor.py   # 手动跑一次 X 监控

# ===== 记忆宫殿 =====
mempalace wake-up   # 恢复项目上下文
mempalace mine      # 索引项目（wing: quant）
mempalace search "关键词"

# ===== Git =====
git push origin HEAD   # 网络不稳时用 ghfast 镜像克隆，推送走官方源
```

### 环境要点

- Python 环境：`/Users/henry/projects/quant/.venv`（Python 3.12，rqalpha editable）
- 代理：交易所/部分数据源需 `http://127.0.0.1:7890`（Clash）
- 新浪接口必须按原始字节 **GBK 解码**（双重转码会丢中文名）
- rqalpha bundle：`~/.rqalpha/`（备份 `bundle-bak-20260731`）
- 凭证纪律：飞书/交易所凭证永不入库、不进 AGENT.md；`config.json`、`*.db` 已入 .gitignore
- 控制台写接口仅监听 localhost

---

## 7. 对话档案索引（docs/handoff_extract/）

| 会话 | 时间 | 主题 |
|---|---|---|
| conv-d87aefc0 | 08-11~08-23 | GitHub 量化调研、mac 券商接口调研、eli5 skill 安装 |
| conv-54d1ff34 | 08-15~16 | X 博主审计、量化博主名单、quant-x-monitor 上线 |
| conv-36a50a64 | 08-16~23 | RQAlpha 环境、动量轮动、KDE 验证链、CCXT/OKX、闭环 v1、控制台、trading_team 首跑、白皮书 |
| conv-4f0f4a56 | 08-22~23 | 七角色团队、日报控制台、审批面板 bug 修复、宏观雷达、quant-wiki 改进建议 |
| conv-49430830 | 08-22~23 | HQCapital 品牌化、角色职责制度化、两层打通、team.db、dev-server、拟人化命名、logo |
| conv-c6508458 | 08-22~23 | 周六晨会实测、bundle 补丁刷新、晨会 token 成本分析 |
| conv-f7f19d23 | 08-22~23 | 日度流水线 Automation 实际运行（轻量模式/周报复盘模式各一次） |
| conv-668bfc79 | 08-23 | execution/ 实盘层建设、miniQMT 申请指南、VM 方案、双 Mac 拓扑 |
| conv-6d633e94 | 08-23 | 策略库扩容 6 策略、sweep 参数扫描、turtle 晋级与冲突、paper dry-run |
| conv-c607c446 | 08-23 | PROJECT_REVIEW 全量整改、门禁 v2、turtle E18 攻关、策略库审查整改（T+1/股票池/Deflated Sharpe/精简）、quant-wiki 建议落地 |
| conv-83641138 | 08-23 | ELI5 系统说明书（HTML 图解） |
| conv-0c232343 | 08-23 | 本交接文档生成 |

---

## 8. 给 Codex 的接手建议

1. **先读三份文件**：`AGENT.md`（全部 Loop 日志）→ `docs/IMPROVEMENT_PLAN.md`（整改执行记录一/二/三）→ `docs/STRATEGY_LIBRARY_REVIEW_A_SHARE.md`（最新审查，含治理原则）。
2. **动手前先 `git status` + `git pull`**：并行会话覆盖是本项目真实发生过的事故。
3. **策略状态变更只走 `pipeline/promote.py`**；门禁改动独立提交；晋级与前向验证证据绑定。
4. **当前 0 live 是刻意状态**，不是故障——Paper 空仓待证中，不要急着恢复任何策略到 live。
5. **改代码后跑 `.venv/bin/python -m pytest tests/ -q`**（134 项基线），改动回测口径后必须全策略重跑门禁。
6. 每个工作单元结束**追加 AGENT.md Loop 条目**（执行/结果/报告/人工决策/下一步），这是项目的记忆中枢。
