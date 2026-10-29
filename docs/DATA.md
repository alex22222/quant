# 数据契约与接入

首个专业数据适配器选择 Tushare Pro，不假定已具备所有权限，不自动购买。官方来源：[财务指标](https://tushare.pro/document/2?doc_id=79)、[申万行业](https://tushare.pro/document/2?doc_id=335)、[指数权重](https://tushare.pro/document/2?doc_id=96)、[权限](https://tushare.pro/document/1?doc_id=108)。

| 数据 | 接口 | 验收要求 |
|---|---|---|
| 全 A 股票表 | stock_basic | L/D/P 分别抓取，含退市/暂停上市 |
| 日线/复权 | daily, adj_factor | 不能只取当前存续股票 |
| 估值/市值/换手 | daily_basic | 研究时点可见 |
| 历史财务 | fina_indicator | 公告可见且保留修订版本 |
| 行业历史 | index_member_all | 申万进出时间，不用当前分类回填 |
| 指数 | index_weight, index_daily | 000300.SH/000905.SH/000852.SH 完整历史快照 |
| 交易状态 | stk_limit, suspend_d, stock_st | 真实涨跌停价格、停复牌、每日 ST 覆盖 |
| 日历 | trade_cal | 实际交易日，不以工作日替代 |

统一保留 `data_date`（经济日期）、`available_date`（保守可见日期）、`observed_at`（本地观测时间）、`source`、`vintage`。查询同时约束经济日期、可见日期和知识截止时间；修订追加不覆盖。公告无时分秒从次日使用。

当前抓到的财务修订、行业退出、指数权重与上市状态不证明过去已知，适配器将可见日期至少推至本地观测日。所有回填标为 `retrospective_vendor_snapshot`，不冒充原始历史版本。首次下载的财务历史可能因此不能直接用于过去的严格 PIT 回测，这是明确的数据缺口。历史行情/复权回填也要披露修订风险。

Parquet 内容寻址并记录文件哈希；DuckDB 先过滤可见性再选版本。空响应不构成覆盖证明；重复分页直接失败。单接口导入不等于全日全市场覆盖。

```bash
# TUSHARE_TOKEN 由环境安全注入，勿放入命令或报告
.venv/bin/python -m research.tushare daily --params '{"trade_date":"20261021"}'
.venv/bin/python -m research.tushare stock_basic --params '{"list_status":"D"}'
.venv/bin/python -m research.tushare fina_indicator --params '{"ts_code":"600000.SH","start_date":"20200101","end_date":"20261021"}'
```

目前只有单接口导入与面板 API，没有已验收的全市场定时同步。DuckDB/pyarrow 安装、令牌权限、退市覆盖、财务原始版本、行业与指数完整性、断点补齐均待验收。分析师预期数据尚未实现。旧 bundle 最新 2026-09-30，仅支持价量探索，不能支持当前交易或完整多因子承诺。

`index_member_all` 默认只返回当前分类；历史接入必须分别请求 `is_new=Y` 和 `is_new=N` 并验收退出覆盖。`ocf_to_profit` 为经营现金流/营业利润，适配器显式请求非默认字段，不能误称净利润口径。

指数权重的 trade_date 也不证明当时的公开发布时间；适配器保守从本地观测日可见，严格历史回测需另验发布档案。
