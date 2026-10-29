# A 股量化研究环境

基于 **RQAlpha v6.3.0** 的 A 股量化研究环境，包含策略回测、Paper 撮合、门禁和研究治理。当前没有任何可实盘策略。

## 目录结构

```
strategies/           策略代码
  base.py               统一信号接口
  momentum_rotation.py  横截面动量研究基准（不可实盘）
  turtle_bluechip.py    冻结参数的趋势突破 Paper 候选
  research/okx_catalog.json  OKX 跨市场知识目录（不可执行）
kde_levels/           KDE 水平支撑阻力位算法与验证
  levels.py             核心算法（Market Profile + KDE + ATR自适应带宽 + 峰值显著性）
  run_compute.py        可视化数据 + 事件研究计算
  out/                  图表与统计结果
youtube_quant_projects.md   量化学习项目清单（含8个开源项目）
```

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e git+https://github.com/ricequant/rqalpha.git#egg=rqalpha
pip install ccxt scipy h5py
rqalpha download-bundle   # 下载免费 A 股日线数据

# 跑回测
rqalpha run -f strategies/momentum_rotation.py \
  -s 2020-01-01 -e 2026-08-01 -bm 000300.XSHG \
  --account stock 100000 --plot-save out.png
```

## 核心结论（2026-08-25）

- 策略库已收敛为 2 个研究对象；其余无效、冗余或伪草案已删除
- 动量轮动仅作研究基准；龟策略只允许冻结参数的纯前向 Paper 观察
- KDE 筹码位触位**无统计预测力**（60 股 × 6.4 万样本日，p > 0.18）
- KDE 位作止损锚能压回撤（28%→20%）但夏普不如不止损；"盈利仓才止损"两头不沾
- 下一个高价值方向是宽股票池指数增强：时点可见数据 + 多因子 + 风险模型 + 成本优化，详见 `docs/A_SHARE_STRATEGY_RESEARCH_2026-08-25.md`

## OKX 跨市场研究库（2026-09-26）

截图中的 11 类策略、26 个可见模板已收录到 `strategies/research/okx_catalog.json`，
在控制台「投研部 · 策略库」下方展示。每项包含原理、收益来源、风险、证据边界、待核实参数和官方来源。
详见 [OKX 策略原理与研究说明](docs/OKX_STRATEGY_LIBRARY_2026-09-26.md)。

这些是研究知识条目，不是新增的 11 个可运行算法；未实现、未回测、未启用，不修改 A 股执行注册表。
截图年化不作为业绩入库，参数未知不使用通用动量草案替代。

## 网络说明

- GitHub 克隆走镜像：`https://ghfast.top/https://github.com/...`
- 交易所 API（OKX 等）需本机代理：`proxies={"http": "http://127.0.0.1:7890", ...}`
