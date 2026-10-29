# 研究架构

主线：专业数据 → 双时间轴 Parquet → DuckDB 可见性查询 → 历史横截面面板 → 因子 → IC/分层/成本诊断 → 滚动验证 → 冻结组合 → 真实前向 Paper。

| 模块 | 职责 | 当前边界 |
|---|---|---|
| research/tushare.py | 分页采集与保守可见日期归一化 | 线上权限/覆盖待验 |
| research/data.py | 追加式 Parquet、内容哈希、DuckDB as-of | 存储集成缺依赖未验 |
| research/panel.py | 历史指数完整快照、行业/财务可见性连接 | 缺失直接拒绝，真实数据待验 |
| pipeline/universe.py | 历史上市时长、ST、停牌、流动性池 | 已接入研究 Loop |
| research/factors.py | 因子、MAD、中性化、IC、分层、换手、期限衰减 | 价量可运行，财务/中性化待完整 PIT 数据 |
| research/portfolio.py | 行业/市值/贝塔暴露约束、TE/换手优化、隔离切分、IR | cvxpy 集成未验，完整滚动拟合待做 |
| research/experiments.py | 执行前登记与真实成功/失败结果 | 新研究、旧回测/网格均接入 |
| paper/execution_model.py | T+1、整手、涨跌停、停牌、历史税费 | 保留；新因子组合尚未接入生产 |

## 事实源

数据以追加式 Parquet 和哈希回执为准；试验只认 `research/experiments.jsonl` 真实事件，报告为派生输出。旧 `status.json` 仅服务历史策略注册表/控制台，旧工作流 ledger 保留历史流程证据，二者不决定新因子结论。尚未迁移遗留账户/工作流事实，不能声称所有双事实源已经合并。

## 冻结

`config/research_policy.json` 缺失、损坏或未知能力均拒绝。入口覆盖晨会、LLM 计划/审批/执行、Multica 新建派单/资源配置/执行复核和计划日终物化；历史只读查询保留。旧测试在显式测试策略下验证历史契约，冻结专用测试使用真实配置。

仓库配置标记暂停，远端 Multica 服务不可达，实际定时器状态未验真；入口拦截不能证明远端已停止派发。未重启共享 daemon。Paper 账户原位保留，不重建、不清空。
