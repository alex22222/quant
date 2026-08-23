# Quant-Wiki 四文阅读 · 当前系统改进建议

> 日期：2026-08-23
> 对象：quant 项目两条线 —— 策略线（`pipeline/` 五阶段 + RQAlpha 门禁）与 LLM 团队线（`trading_team/` 七角色日报 + 批复 + 准确率追踪）
> 来源文章：
> 1. [如何使用 DeepSeek-R1 或 ChatGPT 与 LangChain 构建专业金融分析师](https://quant-wiki.com/ai/%E5%A6%82%E4%BD%95%E4%BD%BF%E7%94%A8DeepSeek-R1%E6%88%96ChatGPT%E4%B8%8ELangchain%E6%9E%84%E5%BB%BA%E4%B8%93%E4%B8%9A%E9%87%91%E8%9E%8D%E5%88%86%E6%9E%90%E5%B8%88/)
> 2. [ChatGPT-quant（LLM 量化交易综合入门）](https://quant-wiki.com/ai/ChatGPT-quant/)
> 3. [ChatGPT-o1（论文→代码自动化）](https://quant-wiki.com/ai/ChatGPT-o1/)
> 4. [chat-paper（Novy-Marx & Velikov 2024, AI-Powered Scholarship / Assaying Anomalies）](https://quant-wiki.com/ai/chat-paper/)

---

## 一、四篇文章的核心洞察（浓缩）

| # | 文章 | 对我们最有价值的三点 |
|---|------|---------------------|
| 1 | LangChain 金融分析师 | ① 技术指标包：RSI/MACD/**Stochastic/VWAP** 全套；② 分析师输出**强制结构化 JSON**（price/technical/financial/recommendation 分块）；③ 新闻逐条提取正文后**逐条情感打分**（Positive/Negative/Neutral），再聚合整体建议 |
| 2 | ChatGPT-quant | ① LLM 交易六步法纪律（选股→策略→回测→评估→风控→部署）；② 绩效必须算年化收益/年化波动/**夏普**；③ **局限性清单**：无实时数据、无法预测突发事件、数据偏见——LLM 结论必须交叉验证、不能替代风控 |
| 3 | ChatGPT-o1 | ① 论文 PDF → 关键词分类（`trading_signal` / `risk_management` 两类句子）→ 自动生成可回测代码的流水线；② prompt 里**硬性注入风控条款**（15% 回撤限制、语法约束、低 temperature）——"从研究到代码的自动化搬运" |
| 4 | chat-paper | ① 「Assaying Anomalies」协议：分组 t 检验 → 多因子回归看 α → 与已有数百异象**去重**；② **多重检验校正（FDR）** 防数据挖掘假阳性；③ 警惕 LLM 批量产出带来的"工业化 HARKing"（先看结果再编假说） |

---

## 二、现状对照

| 维度 | 我们已有 | 差距 |
|------|---------|------|
| 技术指标 | `collect.py` 的 `compute_indicators`：MA/MACD/RSI | 缺 Stochastic、VWAP、布林带 |
| 角色输出 | 七角色自由 Markdown 日报 | 无结构化结论块，`accuracy.json` 回填依赖人工解析 |
| 新闻情绪 | 新闻分析师定性描述；`collect_global.py` 有 7x24 快讯 | 无逐条打分、无聚合温度值 |
| 研究→策略 | quant-x-monitor 在抓量化博主内容；策略有 research 状态机 | 两者未打通，线索靠人工搬运 |
| 风控 | 回测门禁（年化>5%/回撤<30%/夏普>0.3）+ 风控官章程 | LLM 提案侧风控条款靠 prompt 自觉，未模板化硬约束 |
| 防过拟合 | `accuracy.json` T+5 追踪（首个验证日 8-28） | 无样本外验证要求、无与已有因子区分度检验 |
| LLM 局限性护栏 | 数据缺口标注、事实/判断分离、准确率<50% 降权 | 未成文，散在各 prompt 里 |

---

## 三、改进建议（按优先级）

### P0 —— 直接影响准确率自动化闭环

**建议 1：角色输出强制结构化 JSON 结论块**（源自文 1）

- 现状：七角色输出自由 Markdown，T+5 准确率回填需要人读日报判断"看多/看空对不对"。
- 改法：每个角色日报末尾追加固定 JSON 块，例如：
  ```json
  {"role": "technical", "symbol": "600519", "rating": "bullish|neutral|bearish",
   "confidence": 0-100, "key_levels": {"support": ..., "resistance": ...},
   "one_line": "..."}
  ```
- 收益：`accuracy.json` 可全自动回填评级对错；console 可直接渲染评级徽章；研究员/交易员环节可程序化引用上游结论。
- 落点：`trading_team/prompts/01~07` 各加一节输出契约；`plan.json` 派生字段。
- 工作量：小。**建议作为 Loop #13 直接落地。**

### P1 —— 分析质量升级

**建议 2：技术指标扩充**（源自文 1）

- `compute_indicators` 增加：Stochastic Oscillator（%K/%D）、VWAP（日内口径，日线用典型价近似）、布林带（20,2）。
- 技术分析师 prompt（04）相应增加这三个指标的解读要求。
- 落点：`trading_team/collect.py` + `prompts/04_technical.md`。工作量：小。

**建议 3：新闻逐条情感打分 + 聚合温度**（源自文 1b / 文 2）

- 对 `news.json` 与宏观 7x24 快讯，新闻分析师逐条给出 Positive/Negative/Neutral + 分数（-1~1），聚合成"今日舆情温度"单一数值进 `plan.json`。
- 与建议 1 的 JSON 块合并落地，console 宏观条可加一个温度表盘。
- 落点：`prompts/03_news.md` + `build_index.py` / console。工作量：中。

**建议 4：研究线索 → 策略草案流水线**（源自文 3）

- 打通 quant-x-monitor → 策略状态机：对抓到的量化内容做关键词分类（trading_signal / risk_management 两类，参照文 3 的句子分类器），命中 trading_signal 的自动生成 RQAlpha 策略草案，以 `research` 状态入库，走现有回测门禁。
- 这正是 AGENT.md Loop #4 早就挂起的"下一步"，文 3 给出了成熟的分类+生成范式。
- 落点：新增 `pipeline/research_ingest.py` + 策略模板。工作量：大。建议拆两个 Loop。

### P2 —— 纪律与护栏成文

**建议 5：风控条款模板化硬注入**（源自文 3）

- 交易员（06）与风控官（07）prompt 中，把"单票最大回撤 15%、单票仓位上限、总仓位上限"写成不可协商的模板条款（仿文 3 在代码生成 prompt 里硬塞回撤限制的做法），而非现在的原则性描述。
- 落点：`prompts/06_trader.md`、`prompts/07_risk_manager.md`。工作量：小。

**建议 6：防"工业化 HARKing"纪律**（源自文 4）

- 策略入库门禁补充两条：
  1. **样本外验证**：回测区间切出最近 20% 作为样本外，样本外夏普不得低于样本内的 50%；
  2. **因子区分度说明**：新策略必须写明与已有在库策略（动量/120 日线等）的相关性或增量逻辑，防止同义策略重复入库（对应文 4 的"与已有数百异象去重"）。
- LLM 侧沿用既有规则：先有章程假设再看结果，禁止根据已见结果倒推结论写进日报。
- 落点：`pipeline/stage_backtest` 门禁 + `AGENT.md` 规则段。工作量：中。

**建议 7：LLM 局限性护栏成文**（源自文 2）

- 把散落在各 prompt 的护栏（数据缺口必须标注、事实/判断分离、单角色结论不得单独成计划、准确率 <50% 降权）汇总为一节「团队纪律」写进 `trading_team/README` 或 AGENT.md，并加一条文 2 的核心结论：**LLM 输出是研究辅助，执行前必须经风控环节与人工批复**（后者已由批复台账实现，写明即可）。
- 工作量：小。

**建议 8：绩效口径统一**（源自文 2）

- 策略线回测已有年化/回撤/夏普。若未来 LLM 提案进入实盘/paper 执行，提案台账（`approvals.py`）需用同一口径记录每笔提案的后续表现，使"LLM 团队"与"策略池"可横向比较。
- 落点：`approvals.py` + `loops/accuracy.json` 扩展。工作量：中。可等 8-28 首批准确率数据出来后再做。

### 明确不做

**不迁移 LangChain/LangGraph**（文 1 的技术路线）。我们当前的「采集层确定性产出 JSON + LLM 只读数据做判断」与 LangGraph 工具调用殊途同归，但更可控、可复现、幂等，且与 Loop Engineering 的自愈闭环兼容。只借鉴其**输出 schema 约束**思想（建议 1），不引入框架依赖。

---

## 四、建议落地顺序

| 顺序 | Loop 候选 | 内容 | 工作量 |
|------|-----------|------|--------|
| 1 | Loop #13 | 建议 1（结构化 JSON 结论块）+ 建议 5（风控硬条款） | 小+小，一次做完 |
| 2 | Loop #14 | 建议 2（指标扩充）+ 建议 3（新闻打分） | 小+中 |
| 3 | Loop #15 | 建议 6（样本外+区分度门禁）+ 建议 7（纪律成文） | 中+小 |
| 4 | Loop #16~17 | 建议 4（研究→策略流水线，拆两步） | 大 |
| 5 | 择机 | 建议 8（绩效口径，等 8-28 数据） | 中 |

> 原则：每个 Loop 仍按 AGENT.md 协议走——小步、可验证、console 可见、记录归档。
