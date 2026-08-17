# 视频《量化交易基础、工具选择、学习建议》提到的学习项目清单

- **视频**: [量化交易基础、工具选择、学习建议](https://www.youtube.com/watch?v=fH4KbFWkTWM)
- **频道**: Find Interesting AI（@architect-felix）
- **整理日期**: 2026-08-16（Stars 与更新时间为整理当日的 GitHub 实时数据）

## 项目总表

| # | 项目 | 链接 | 解决的任务 | 视频中时间点 | 视频要点 | Stars（2026-08） | 最近更新 |
|---|------|------|-----------|-------------|---------|------------------|----------|
| 1 | **yfinance** | https://github.com/ranaroussi/yfinance | 数据获取（**美股**） | 3:46 | `pip install yfinance`；输入股票代码即可拿公司元数据、高管信息、历史行情（开高低收量） | 24,994 | 2026-08-13 |
| 2 | **efinance** | https://github.com/Micro-sheep/efinance | 数据获取（**A股/基金/债券/期货**） | 5:22 | `pip install efinance`；演示获取贵州茅台历史行情、主力/小单净流入等资金流向数据 | 3,939 | 2026-07-17 |
| 3 | **vnpy（VeighNa）** | https://github.com/vnpy/vnpy | **交易执行** | 6:40 | 封装国内券商/期货/ETF 期权及海外市场接口；已演化成带界面的 VN Station 软件，可界面化交易；正横向扩展到数据与 AI 能力 | 44,497 | 2026-08-10 |
| 4 | **Qlib** | https://github.com/microsoft/qlib | **全栈框架**：数据+策略+回测+交易 | 8:13 | 微软开源的 AI 量化平台；`pip install pyqlib`，官方提供数据下载，可直接跑回测、训练策略；适合有策略开发能力的人 | 47,460 | 2026-07-23 |
| 5 | **CCXT** | https://github.com/ccxt/ccxt | **加密货币**交易与数据 | 12:52 | 统一封装 100+ 交易所（币安等）的行情与下单 API；视频演示打印支持的交易所列表、币安 features、BTC/USD 行情 | 43,637 | 2026-08-16 |
| 6 | **AI Hedge Fund** | https://github.com/virattt/ai-hedge-fund | **AI 智能体投研** | 19:26 | 模拟多位"交易名人"智能体（如查理·芒格）+ 风险管理经理，由 AI 完成分析并给出买卖决策；需配置大模型 API Key（视频用 DeepSeek）；优点是傻瓜化，缺点是过程黑盒 | 62,879 | 2026-08-07 |
| 7 | **AI Quant Trade** | https://github.com/charliedream1/ai_quant_trade | **学习资源库** | 22:19 | 资源分享型项目：股票知识、策略实例、因子挖掘、大模型/机器学习/深度学习，从学习、模拟到实盘一站式 | 6,273 | 2026-08-16 |
| 8 | **Quant Wiki** | https://github.com/LLMQuant/quant-wiki | **量化知识库** | 22:44 | 收集量化基础概念、入门内容、前沿论文、工具资料，适合系统性快速成长 | 4,046 | 2026-04-16 |

## 视频的分类逻辑（按量化任务划分）

```
数据获取 ──── yfinance（美股） / efinance（A股）
交易执行 ──── vnpy（股票/期货，带界面） / CCXT（加密货币）
全栈框架 ──── Qlib（数据→策略→回测→上线，微软）
AI 新范式 ── AI Hedge Fund（名人智能体替你分析决策）
学习资源 ──── AI Quant Trade（资源库） / Quant Wiki（知识库）
```

## 补充说明

- 视频还口播提及 **QMT**（15:44，券商量化终端，不写代码做策略的代表），属于商业软件，无开源链接，未列入上表。
- 视频发布时 AI Hedge Fund 为 20K stars，当前已达 62.9K，是 8 个项目中增长最快的。
- 作者给出的选型建议：交易股票/基金选 Qlib，加密货币选 CCXT；两者都需要编码能力（无界面）；不想写代码可考虑 QMT 类软件。
- 作者提醒：AI 智能体类工具"赚钱/亏钱的原因完全黑盒，好不好仁者见仁"。
