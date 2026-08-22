# execution —— A 股实盘执行层（平安证券 · easytrader 过渡方案）

> 状态：**演练模式已全链路验证**；实盘模式待 Windows 环境部署后人工验证。
> 纪律：默认 dry-run；live 必须同时满足 ① config.json `live_enabled=true` ② CLI 显式 `--mode live`。

## 架构

```
signals.py        目标仓位信号（与 pipeline/stage_paper.py 同一 momentum_rotation 逻辑）
market.py         腾讯实时行情（GBK 解码；定价 + 涨跌停估算）
order_manager.py  目标仓位 → 订单：整手 / T+1 可卖 / 涨跌停拦截 / 先卖后买 / 滑点限价
risk_guard.py     下单前门禁：单票 ≤35% / 总仓 ≤98% / 日亏 3% 熔断 / 订单数上限 / 脏价格 / 黑名单
broker_base.py    Broker 抽象（balance / positions / buy / sell / cancel / today_entrusts）
broker_mock.py    MockBroker：SQLite 模拟券商，挂单价到位即成交（演练用）
broker_easytrader.py  EasyTraderBroker：同花顺通用下单客户端（universal_client，仅 Windows）
ledger.py         execution/execution.db：orders / daily 两表，执行线事实源
notifier.py       飞书卡片推送（复用 quant-x-monitor/config.json 凭证，不落日志）
runner.py         主循环 CLI：信号→行情→计划→门禁→下单→对账→台账→飞书，同日同 mode 幂等
tests/test_guard.py   10 条风控/订单边界用例（离线）
```

## 日常运行

```bash
# 演练（MockBroker，任何平台可跑）
.venv/bin/python -m execution.runner --mode dry-run

# 指定外部信号（{"600519.XSHG": 0.33, ...}）
.venv/bin/python -m execution.runner --mode dry-run --signal my_signal.json

# 离线自测（无网络）
.venv/bin/python execution/tests/test_guard.py
```

幂等：同一交易日同一 mode 只执行一次，`--force` 覆盖。非交易时段 live 拒绝执行，`--force` 覆盖。

## 实盘部署清单（Windows，一次性）

1. **环境**：Windows 云服务器或本地 Windows 机；交易时段保持开机、不锁屏（pywinauto 需要可见桌面会话）。
2. **安装同花顺下单客户端**（xiadan.exe），用平安证券资金账号登录一次，确认能手动下一笔单。
   - ⚠️ 需先验证：平安证券是否支持同花顺通用下单。若不支持，备选：平安证券 PC 客户端的通达信内核 + easytrader 对应 client，或更换支持 QMT 的券商（见 README 决策记录）。
3. `pip install easytrader`（建议 Python 3.10+，Windows 官方包）。
4. 克隆本仓库到 Windows，`cp execution/config.example.json execution/config.json`，填：
   - `broker: "easytrader"`
   - `xiadan_path`: xiadan.exe 绝对路径（或留空自动匹配已运行客户端）
   - `live_enabled: true`（**打开前先用 dry-run 在 Windows 上跑通一次**）
5. **实盘前验证清单**（全过才开 live）：
   - [ ] `python -c "from execution.broker_easytrader import EasyTraderBroker; b=EasyTraderBroker(); b.connect(); print(b.balance(), b.positions())"` 返回真实账户数据
   - [ ] 手动在同花顺客户端下一笔 100 股买单并撤单，确认 easytrader 能看到当日委托
   - [ ] `--mode dry-run` 跑一天，对照飞书卡片与客户端持仓
   - [ ] 首笔 live 用最小仓（单票 ≤1 手）验证成交回报 → `execution.db` 订单状态 = filled
6. **定时**：Windows 任务计划程序，每个交易日 09:35 / 14:50 各跑一次 `python -m execution.runner --mode live`（早盘建仓窗口 + 尾盘确认窗口）。

## 安全边界

- 凭证纪律：飞书凭证只从 quant-x-monitor/config.json 读；券商登录态在同花顺客户端本地；两者都不入库、不进日志、不进 git（.gitignore 已覆盖）。
- easytrader 是 UI 自动化，**同花顺客户端升级可能导致字段漂移**——`broker_easytrader._col()` 已做多候选名兜底；若实盘取数异常，先看字段名是否变更。
- 日亏 3% 熔断、涨停不买/跌停不卖、单票 35% 上限是硬规则，调整需改 config.json 并在 AGENT.md Loop 日志留痕。
- easytrader 只是过渡；资金量到 10 万档后建议迁移 miniQMT（xtquant），Broker 接口不变，新增 `broker_xtquant.py` 即可。

## 已知简化（v1）

- 涨跌停价按昨收 ±幅度估算，除权除息日有误差（风控层 max_price_dev 双保险）。
- 委托对账只读当日委托回报，不做逐笔成交回报撮合（easytrader 不支持逐笔推送）。
- MockBroker 撮合简化为「挂单价穿越最新价即成交」，不模拟排队。
