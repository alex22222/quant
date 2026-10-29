# A 股因子研究环境

目标：用可复现的数据与因子证据验证中证 500 / 中证 1000 指数增强。当前没有过门禁的策略，没有已证实的可交易 alpha。LLM 晨会、Multica 复核和交易计划链已在代码入口冻结；LLM 仅保留研究摘要用途。

## 研究 Loop

```bash
# 完整依赖；当前沙箱不能下载，不代表存储和优化器已经验收
.venv/bin/python -m pip install -r requirements-research.txt
# 可离线运行：历史股票池 -> 价量因子 -> IC/分层/换手/衰减 -> 实验台账
.venv/bin/python -m research.run --start 2024-01-01 --end 2026-09-30 --universe-size 500
# 默认仅扫描本项目
.venv/bin/python -m pytest -q
```

价量基线用本地 RQAlpha bundle，明确不含行业/市值中性化、指数增强业绩或成交验证。报告位于 `artifacts/research/`，真实试验位于 `research/experiments.jsonl`。旧 `pipeline.run` 仍是历史策略流程，不是本次研究命令。

## 五份主文档

本 README，以及 [ARCHITECTURE](docs/ARCHITECTURE.md)、[DATA](docs/DATA.md)、[RESEARCH](docs/RESEARCH.md)、[CHANGELOG](docs/CHANGELOG.md)。旧文档和完整 AGENT/LOOP 日志保留于 `docs/archive/2026-10-22/`；[原始评审](docs/PROJECT_REVIEW_2026-10-22.md) 单独保留。两个旧运行手册路径仅为兼容链接。

本地日线实测截止 2026-09-30，检查时滞后 22 天。专业数据尚未下载。环境变量 `TUSHARE_TOKEN` 不写日志、不入库、不自动购买权限。实盘入口已停用，`--force` 和 `live_enabled=true` 不能恢复 easytrader；未来 QMT/PTrade 另行评审。三个月真实前向 Paper 不能用历史回放替代。
