#!/usr/bin/env python3
"""根据 quant-x-monitor/reports/ 按日存档生成 console/news/ 每日总结 + index.json
总结正文由 agent 撰写（LLM 生成），本脚本只负责落盘与索引。
"""
import json, os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # quant/
NEWS_DIR = os.path.join(BASE, "console", "news")
REPORTS_DIR = os.path.join(BASE, "quant-x-monitor", "reports")

SUMMARIES = {
    "2026-08-10": """## 要点
- **代币化股票 24×7**：CZ 转发 Richard Teng 关于代币化股票市场的推文，强调「24x7、低费率、高效、全球、开放市场」——代币化股权交易机制持续升温。
- **托管安全之争**：CZ 转发 Willy Woo 的 BTC 丢失数据，称「统计数据显示把币放交易所比自托管更安全」。
- **泰国免税**：泰国确认对比特币和加密货币实行 0% 资本利得税。
- TRON 总交易突破 150 亿笔；火币 HTX 推美股永续合约（负费率 + 8 万 USDT 奖池）。

## 量化相关性
低。无策略研究内容，代币化股票 24×7 交易机制可作为市场微观结构演变的背景关注。""",

    "2026-08-11": """## 要点
- 波场 USDT 发行量达 912 亿，登顶 USDT 第一大网络。
- CZ 再次强调泰国对加密货币 0% 资本利得税。

## 量化相关性
低。稳定币链上份额变化可作为加密市场资金流向的旁证指标。""",

    "2026-08-12": """## 要点
- 波场 USDT 登顶话题延续。
- Musk 证实其含糊推文与 SpaceX 相关（非 TSLA 股价）。

## 量化相关性
低。无实质策略或市场信息。""",

    "2026-08-13": """## 要点
- 波场 USDT 以 912 亿发行量创历史新高，TRON 跃居 USDT 第一大网络。
- 火币 HTX 美股永续合约交易挖矿第 2 期：负费率 + 80,000 USDT 奖池。
- Musk：Grok Build 能力强大。

## 量化相关性
低。美股永续合约在加密交易所推广，反映跨市场产品线竞争。""",

    "2026-08-14": """## 要点
- **bstocks 上线两个月占全球代币化股权市场 27%**（赵长鹏）——代币化股票赛道增长惊人，值得跟踪其对市场结构的长期影响。
- 孙宇晨：「AI 时代才刚刚开始，现在就像 2007-2008 年 iPhone 刚出来的时候」。
- 波场 USDT 登顶话题延续；Grok Build 持续预热。

## 量化相关性
中。代币化股权 27% 市占是本月最值得注意的市场结构信号。""",

    "2026-08-15": """## 要点
- Musk：Grok 4.6 将与 Grok Build harness 配合最佳。
- CZ 推荐其新书《Freedom of Money》。
- 孙宇晨：「投资就得看长期回报，任何行业最后都要拿结果说话」。

## 量化相关性
低。无策略内容。""",

    "2026-08-16": """## 要点（量化含量高 ⭐）
- **黄金 × 比特币双重动量**（Quantocracy 转 @AliAskar92）：在两种「价值储藏」资产间做动量轮动的研究，探讨避险资产趋势切换信号。
- **VIX 与趋势跟踪重访**（Alpha Architect）：基于近十年样本外证据检验 VIX 信号在趋势跟随中的有效性。
- **因子择时大多失败**（@AliAskar92）：剔除数据偏差的「诚实版」回测下，多数因子择时策略不显著——对策略开发是重要警示。
- **Meb Faber**：全球深度价值股过去 1/3/5 年均跑赢 SPY，但只要 SPY 维持年化 15% 投资者就不在乎——行为金融观察。
- 加密侧：TRON Q2 回购销毁 3475 万美元。

## 量化相关性
高。三条策略研究 + 一条行为观察，其中「因子择时诚实检验」直接呼应本项目回测门禁设计。""",

    "2026-08-17": """## 要点（量化含量高 ⭐）
- **行业 ETF 月内动量周期**（Quantpedia）：美国行业 ETF 存在持续的「月末效应」动量模式，按正确顺序利用可获有意义的风险调整后收益——可作为 A 股行业轮动的对照研究。
- **价格路径凸性新异象**（@AliAskar92）：价格路径凸性特征在截面选股中呈现显著溢价。
- **AI 研究代理 = 未披露的因子暴露**（Jonathan Kinlay）：趋同的模型与数据让 AI 策略隐含共同风险——对 LLM 驱动的投研流程是直接警告。
- Laevitas 发布本周加密衍生品市场展望。
- Meb Faber 推荐 Luke Gromen 播客：「AI 对联邦预算意味着什么」。

## 量化相关性
高。行业动量、新异象、AI 因子拥挤三条均有策略参考价值。""",

    "2026-08-18": """## 要点
- Musk：Grok 4.6 在医疗 Agent 基准测试登顶。
- 孙宇晨晒 8 月 16 日「表现不错」。

## 量化相关性
低。无实质内容。""",

    "2026-08-19": """## 要点
- **BTC 已挖出 2007 万枚**（CZ）：仅剩 4.4% 供应量，估计 10-20% 已永久丢失——通缩叙事强化。
- DeepSeek-V4 涨价之际 B.AI 反向限时免费（孙宇晨转发支持）。

## 量化相关性
低-中。BTC 供给数据是长期估值模型的输入之一。""",

    "2026-08-21": """## 要点
- 当日 8 条均为孙宇晨 / CZ / Musk 动态，无新增量化研究内容。
- CZ 澄清伪造聊天记录：「这是假的，造谣者现在公然撒谎」。

## 量化相关性
无。信噪比最低的一天——监控账号配置已于 8-16 切换为量化博主，此前抓取的币圈账号内容属历史遗留噪音。""",

    "2026-08-22": """## 要点（量化相关 ⭐）
- **Corey Hoffstein 回应质疑**：承认旗舰策略借鉴了 40 年前的老想法，自成立以来年化 19.5%，从未购买媒体曝光——「老想法 + 好执行」的又一例证。
- **Meb Faber 两条 All Weather**：桥水 × 道富的 ALLW ETF 债券仓位达 111%（杠杆）；另一条直呼「尴尬了」暗示全天候策略近期承压——风险平价类产品在高利率环境下的结构性问题值得跟踪。
- 加密侧：波场总转账额突破 29 万亿美元。

## 量化相关性
中-高。Hoffstein 的策略自辩与 ALLW 杠杆结构都对多资产配置研究有参考。""",
}

def main():
    os.makedirs(NEWS_DIR, exist_ok=True)
    index = []
    dates = sorted(SUMMARIES.keys(), reverse=True)
    for date in dates:
        rpt = os.path.join(REPORTS_DIR, f"{date}.json")
        items = json.load(open(rpt, encoding="utf-8")) if os.path.exists(rpt) else []
        authors = sorted({it.get("author", "") for it in items if it.get("author")})
        md = f"# 量化新闻 · {date}\n\n> 共 {len(items)} 条 · 来源 X/Twitter · 监控 {len(authors)} 个账号\n\n{SUMMARIES[date]}\n"
        with open(os.path.join(NEWS_DIR, f"{date}.md"), "w", encoding="utf-8") as f:
            f.write(md)
        # 摘要首行非空要点作为 headline（去除 markdown 加粗标记）
        headline = next((l.strip("- *#> ") for l in SUMMARIES[date].split("\n")
                         if l.strip().startswith("- ")), "").replace("**", "")
        index.append({"date": date, "count": len(items), "headline": headline})
    with open(os.path.join(NEWS_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"[News] 生成 {len(dates)} 天总结 -> console/news/", file=sys.stderr)

if __name__ == "__main__":
    main()
