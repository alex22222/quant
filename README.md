# A 股量化实验场

基于 **RQAlpha v6.3.0** 的 A 股量化策略研究环境，包含策略回测、KDE 支撑阻力位算法的完整验证链（复现 → 统计检验 → 实盘级回测裁决）。

## 目录结构

```
strategies/           策略代码
  demo_dual_ma.py       双均线示例（环境验证用）
  momentum_rotation.py  多股票动量轮动（月度调仓 + 120日线风控）
  momentum_stops.py     动量轮动 + 四种止损机制对照（none/atr/kde/kde_profit）
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
rqalpha run -f strategies/momentum_stops.py \
  -s 2020-01-01 -e 2026-08-01 -bm 000300.XSHG \
  --account stock 100000 --extra-vars '{"stop":"kde"}' \
  --plot-save out.png
```

## 核心结论（2026-08 验证）

- 动量轮动基线：年化 10.4%，夏普 0.458，最大回撤 28.2%
- KDE 筹码位触位**无统计预测力**（60 股 × 6.4 万样本日，p > 0.18）
- KDE 位作止损锚能压回撤（28%→20%）但夏普不如不止损；"盈利仓才止损"两头不沾

## 网络说明

- GitHub 克隆走镜像：`https://ghfast.top/https://github.com/...`
- 交易所 API（OKX 等）需本机代理：`proxies={"http": "http://127.0.0.1:7890", ...}`
