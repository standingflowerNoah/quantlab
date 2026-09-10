# QuantLab — Agent 端 A 股量化研究系统

> 五层架构：L1 数据层 → L2 因子层 → L3 模型层 → L4 优化层 → L5 决策层
> 当前版本：**L1~L5 五层全部建成**（数据→因子→模型→优化→决策）

生产选股模型：`size + amihud_20` 双因子等权 top100（基准中证1000），每日流水线自动跑
数据更新 → 质量闸门 → 因子+审查 → 决策信号 → 总览报告 → 数据看板 → 前向监控 七步，
配套 FastAPI 只读研究网关（`scripts/api_server.py`）支持多人协作查询。

## 新人上手四步路线

> 本仓库为 GitHub 私有仓库，需所有者邀请你为 Collaborator 后才能克隆。

### 第 1 步 · 克隆代码

```bash
git clone https://github.com/standingflowerNoah/quantlab.git C:\quantlab
cd C:\quantlab
```

仓库只含代码与研究文档（~10MB）；`data/`、`reports/`、`tools/` 不入库，数据按下文第 3 步获取。

### 第 2 步 · 环境配置（Windows，约 30 分钟）

```bash
# 1) Python 3.13 + 专用 venv（pypi 走清华镜像，国内网络必须）
python -m venv C:\quantlab\envs\quantlab
C:\quantlab\envs\quantlab\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2) 冒烟测试（不依赖数据湖也应通过）
C:\quantlab\envs\quantlab\Scripts\python.exe tests/test_smoke.py
```

依赖要点（全部已固化在 `requirements.txt`）：`duckdb 1.5.5`（存储引擎）、`pytdx`（通达信行情）、
`pandas/numpy`、`fastapi+uvicorn`（研究网关）。分钟级数据依赖 free-stockdb Windows 工具链
（`tools/free-stockdb/win/stockdb/`），只跑日频研究可不装。

### 第 3 步 · 复刻数据到本地（两条路线，详见 `docs/collab_setup.md`）

| 路线 | 做法 | 体量 | 适用 |
|---|---|---|---|
| **A 全量拷贝**（推荐） | 移动硬盘/网盘拷贝三块：fsdb 镜像 24G + 分钟湖 6.5G + 因子湖 9.5G（日K DuckDB 与湖一起带走） | ~41GB | 追求与生产环境数字完全一致 |
| **B 远程重建** | `$PY cli.py data init` 全量首建 + fsdb 镜像同步 + `$PY cli.py factor compute-all` | 天然一致但耗时数小时~1天 | 拿不到硬盘时 |

> 只跑日频研究：路线 B 的 `data init`（~20GB 增量到 2022-01）即可覆盖全部日频因子与模型。
> 数据就位后必跑验证清单：`data status` → `data quality` → `tests/test_smoke.py`，数字应与
> 路线 A 完全一致（同源同算法）；差异大先查 `data quality`。

### 第 4 步 · 全面理解系统（按序阅读）

| 顺序 | 材料 | 内容 |
|---|---|---|
| ① | **`docs/quantlab_guide.html`** | 图文讲解材料：研究流程闭环 → 五层架构与数据流转（含架构图）→ reversal_5 八步上手实操，**新人从这里开始** |
| ② | 本 README 下文各层章节 | 五层各自的命令与实测结果 |
| ③ | `research/*.md` | 13 篇研究主线文档：正面/负面结论全部归档（诚实归因路线） |
| ④ | `reports/`（overview_report.html 等） | 每日流水线产出的总览报告与数据看板 |

文档导航：`docs/architecture.html`（架构设计）· `docs/DEV_LOG.md`（迭代日志）·
`docs/collab_setup.md`（协作环境与数据分发）· `docs/service_deploy.md`（服务化部署/API 网关）

## 快速上手

```bash
cd quantlab
PY="C:\Users\53497\.workbuddy\binaries\python\envs\quantlab\Scripts\python.exe"

# 数据状态总览（各域水位/行数/新鲜度）
$PY cli.py data status

# SQL 直接查数据湖（写锁被占时自动降级 Parquet 镜像）
$PY cli.py query "SELECT COUNT(*) FROM kline_daily"

# 复权因子校验（默认茅台，可 --code 换股）
$PY cli.py data test-adj --code 600519

# 股票池规模一览
$PY cli.py universe
$PY cli.py universe hs300        # 指定池代码数

# 全量首建（幂等断点续跑）
$PY cli.py data init

# 日度增量更新（--domain 可指定子集）
$PY cli.py data update
$PY cli.py data update --domain kline_daily,daily_snapshot

# 数据体检（完整性/新鲜度/合理性/一致性/复权抽样）
$PY cli.py data quality --sample 30

# 因子相关性矩阵（识别冗余因子）
$PY cli.py factor corr

# 样本内外验证（walk-forward）
$PY cli.py model walkforward --split 2025-01-01

# 每日一键流水线（数据更新→质量闸门→因子+自动审查→决策→总览→看板→前向监控，共七步）
$PY scripts/daily_pipeline.py

# 因子审查（全量/强制重审/单因子；报告 reports/factor_audit.html）
$PY scripts/factor_audit.py all
$PY scripts/factor_audit.py --names size,dragon_net_20 --force

# 纸面组合前向监控（reports/forward_monitor.html）与容量测算
$PY scripts/forward_monitor.py
$PY scripts/capacity_analysis.py

# 模型对比报告（等权3因子 vs LightGBM）
$PY scripts/model_comparison.py

# 冒烟测试（回归保护）
$PY tests/test_smoke.py

# FastAPI 只读研究网关（多协作查询；QUANTLAB_API_TOKEN 自设，见 docs/service_deploy.md）
$PY scripts/api_server.py --port 8000
```

> 开发日志见 `docs/DEV_LOG.md`（记录了各轮迭代的优化与验证结论）。

## 内置股票池

| 池名 | 说明 |
|---|---|
| `all` | 全部 A 股（含 ST/北交所） |
| `ashare_ex` | 全 A 剔除 ST/次新(<120日)/北交所 ← **研究默认** |
| `ashare_main` | 全 A 剔除 ST/次新，保留北交所 |
| `hs300` / `sz50` / `zz500` / `zz1000` / `zz2000` | 指数成分（东财 datacenter，周更） |
| `top500` / `top2000` / `top3000` | 流通市值排名动态池 |

## 数据域清单（P1）

| 域 | 表 | 源 | 频率 |
|---|---|---|---|
| 日K线+复权因子 | `kline_daily` / `dividend_events` | 通达信 TCP | 日 |
| 指数日K | `index_kline` | 通达信 TCP | 日 |
| 交易日历 | `trade_calendar` | 自维护 | 年 |
| 股票主表 | `instruments` | 新浪+通达信 | 周 |
| 当日估值快照 | `daily_snapshot` | 腾讯行情 | 日 |
| 财务快照 | `finance_snapshot` | 通达信 | 周触发 |
| 龙虎榜 | `dragon_tiger` | 东财 datacenter | 日 |
| 两融余额 | `margin_total` | 东财 datacenter | 日 |
| 解禁 | `lockup` | 东财 datacenter | 日 |
| 大宗交易 | `block_trade` | 东财 datacenter | 日 |
| 股东户数 | `holder_num` | 东财 datacenter | 月触发 |
| 热点题材 | `hot_topic` | 同花顺 | 日 |
| 北向资金 | `northbound_daily` | 同花顺 | 日 |
| 指数成分 | `index_members` | 东财 datacenter | 周触发 |
| 个股资金流 | `fund_flow_daily` | 东财 push2his | degraded（探测恢复自动启用） |

## 因子层（P2）

因子统一契约：`date / code / value` 长格式，Parquet 按年分区存储（`data/lake/factor/<name>/`）。

```bash
# 因子清单
$PY cli.py factor list

# 计算因子（幂等写盘；--universe 可限股票池）
$PY cli.py factor compute momentum_20 --universe ashare_ex
$PY cli.py factor compute-all                # 批量计算全部因子

# 因子质量评估：IC = 因子值 vs 未来收益的截面秩相关
$PY cli.py factor summary momentum_20 --horizon 5
$PY cli.py factor ic reversal_5 --horizon 5   # 逐日 IC 序列

# 分层回测 + 多空组合（非重叠持有期，避免复利虚高）
$PY cli.py factor backtest reversal_5 --horizon 20 --quantiles 5
```

### 内置因子（38 个）

| 因子 | 类别 | 说明 |
|---|---|---|
| `momentum_20/60/120` | price | N 日动量（前复权累计收益） |
| `reversal_5/10` | price | 短期反转（-N 日收益） |
| `volatility_20/60` | price | 日收益波动率（标准差） |
| `rsi_14` | tech | 相对强弱指标 |
| `amplitude_20` | price | 20日平均振幅 (high-low)/close |
| `max_return_20` | price | 20日最大单日收益（追涨风险） |
| `price_position_250` | price | 250日价格位置 close/250日最高 |
| `size` / `total_mcap` | size | 对数流通/总市值（close×股本） |
| `turnover` | volume | 日换手率（vol×100/流通股本） |
| `value_pe` / `value_pb` | value | PE/PB（当前截面，历史需快照积累） |
| `roe` | quality | 净资产收益率 net_profit/net_assets |
| `op_margin` | quality | 营业利润率 operating_profit/revenue（口径已实证校验） |
| `debt_ratio` | quality | 资产负债率（杠杆风险） |
| `ocf_ratio` | quality | 经营现金流/净利润（盈利含金量） |

**国泰君安经典因子（新增 18 个，见 `reports/gtja_report.html`）**：

| 类别 | 因子 | 说明 |
|---|---|---|
| 流动性 liquidity | `amihud_20` / `turnover_std_20` / `avg_amount_20` / `amount_std_20` | Amihud 非流动性（低流动性溢价）、换手波动、成交额规模/波动 |
| 价格 price | `downside_volatility_20` / `skewness_20` | 下行波动率、收益偏度（彩票效应） |
| 估值 value | `ep` / `bp` / `sp` / `ocfp` | 盈利/账面/销售/现金流 市值比（财务截面） |
| 质量 quality | `roa` / `net_margin` / `asset_turnover` | 总资产收益率、净利率、资产周转率 |
| 杠杆 leverage | `current_ratio` / `quick_ratio` | 流动比率、速动比率 |
| 成长 growth | `revenue_growth_yoy` / `profit_growth_yoy` / `asset_growth_yoy` | 同比增速（需财务≥2期，当前数据不足） |

> 质量/估值/杠杆/成长因子基于 finance_snapshot 财务快照（当前仅 2026-06-30 单期），
> 为当前截面因子，历史深度随财务快照周更积累。
> 注意：通达信 zhuyinglirun（主营利润）字段语义不可靠，已弃用原 gross_margin，
> 改用营业利润率 op_margin（茅台/五粮液/宁德/海康 4 家抽查与公开数据吻合）。

### 国泰君安 Alpha191（短周期价量因子库）

《基于短周期价量特征的多因子选股体系》191 因子，独立于 registry 管理（不参与每日 compute_all）：

```bash
# 批量计算落库（与 data update 串行，DuckDB 单写者）
$PY scripts/alpha191_compute.py calc                 # 全量 191 个
$PY scripts/alpha191_compute.py calc --range 1-50    # 指定编号段
# 批量 IC 评估（horizon 5/20）
$PY scripts/alpha191_compute.py ic
# 研究总报告（公式覆盖/IC/去冗余/组合与融合检验）
$PY scripts/alpha191_report.py
```

- 引擎：`quantlab/factor/alpha191.py`（宽表向量化算子 `alpha191_ops.py` + 公式库 `alpha191_formulas.py`）
- 结果：180/191 落库（~9 亿行）；72 个去冗余强代表（|ICIR20| 最高 0.81）
- 结论：IC 层面有效（量价背离族），但组合层面（等权/加权融合/池内精选）均跑输
  `size+amihud_20`——A 股小市值+低流动性溢价幅度远大于短周期量价 alpha，
  全套作为信号资产留档（详见 `reports/alpha191_report.html`）

### 实测 IC（2022-01 ~ 2026-09，全 A 去 ST）

| 因子 | IC(5日) | IC(20日) | 效应 |
|---|---|---|---|
| `reversal_10` | +0.046 | +0.062 | 短期反转（正） |
| `turnover` | -0.078 | -0.095 | 低换手溢价（负） |
| `volatility_20` | -0.074 | -0.092 | 低波动异象（负） |
| `momentum_60` | -0.064 | -0.086 | 动量反转（负） |
| `size` | -0.030 | -0.053 | 小市值溢价（负） |

> 以上均符合 A 股经典实证规律：短期反转 + 低波动 + 低换手 + 小市值。

### 分层回测（horizon=20日，5层，多空 Q5−Q1 年化）

| 因子 | Q1 年化 | Q5 年化 | 多空年化 | 单调性 |
|---|---|---|---|---|
| `reversal_5` | 1.3% | 15.2% | **+13.8%** | 0.7 |
| `size` | 28.2% | 3.5% | **−19.6%** | −1.0 |
| `turnover` | 15.9% | −3.8% | −17.2% | −0.7 |
| `momentum_60` | 21.3% | 3.7% | −14.7% | −0.9 |
| `volatility_20` | 13.9% | 3.7% | −9.0% | −0.7 |

> 多空年化正 = 做多高因子值；负 = 反向做多低因子值。全部与 IC 方向一致。

## 模型层（P3）

多因子合成 + 截面选股回测：

```bash
# 多因子选股回测（默认 reversal_5+size+turnover，top50，20日调仓）
$PY cli.py model backtest
$PY cli.py model backtest --factors reversal_5,size,turnover --n 100 --rebalance 20
```

### 合成逻辑
1. 每日截面 rank 标准化到 [0,1]（抗异常值）
2. 方向校正（负 IC 因子取反，见 `model/composite.py` 的 FACTOR_DIRECTION）
3. 等权合成 → 综合评分，取 top N 等权持有

### 实测绩效（2022-01 ~ 2026-09，已扣佣金/印花税/滑点）

| 组合 | 年化 | 超额年化 | 夏普 | 最大回撤 |
|---|---|---|---|---|
| 3因子 top100 | 25.8% | 26.4% | 0.45 | -36.4% |
| size 单因子 | 35.2% | 35.9% | 0.44 | -41.3% |
| 5因子 top50 | 24.3% | 24.9% | 0.40 | -43.3% |

> 小市值(size)是核心 alpha 来源，换手仅 15%；多因子合成提升分散度。

## 优化层（P4）

组合权重优化（纯 numpy，无额外依赖）：

```bash
# 对比全部权重方案
$PY cli.py optimize compare

# 单方案回测（--method equal/inverse_vol/min_var/risk_parity）
$PY cli.py optimize backtest --method inverse_vol --n 100
```

### 实测（3因子 top100，20日调仓）

| 方案 | 年化 | 夏普 | 最大回撤 |
|---|---|---|---|
| 等权（基线） | 28.1% | 0.49 | -35.6% |
| **逆波动** | **32.4%** | **0.57** | -35.1% |
| 最小方差 | 22.8% | 0.47 | **-34.0%** |
| 风险平价 | 28.0% | 0.56 | -40.6% |

> 逆波动加权全面占优（夏普 +0.08）；最小方差最抗跌。不改选股仅改权重即改善。

## 决策层（P5）

评分 → 信号 → 调仓清单的每日闭环：

```bash
# 生成目标持仓（top100 + 逆波动权重 + 个股/行业风控）
$PY cli.py decision target

# 对比当前持仓生成调仓指令（幂等）
$PY cli.py decision rebalance

# 完整流程：目标 → 调仓 → 持久化持仓状态
$PY cli.py decision run
```

- 默认选股池 `ashare_ex`（去 ST/次新/北交所）
- **选股模型：`size + amihud_20`**（小市值 + 低流动性溢价，rank 等权，均做多低值，
  统一入口 `quantlab/model/pool_select.py::build_production_score`）。
  全期年化 32.3%/夏普 0.98/IR 1.64；可成交性约束（剔除建仓日涨幅>7%股）后
  年化 +38.3%→32.0% 仍稳健。见 `reports/gtja_report.html`、`hier_oos_fixed.json`。
  > ⚠️ 曾用「池内精选」（dragon_net_20 精选，回测年化 45.7%）——2026-09-05 前视
  > 审计发现 dragon 因子窗口方向写反（t 日值含未来事件，ICIR 虚高 2.3），
  > 全部数字作废并回退。修复后 dragon 实测负 IC（上榜后均值回归），
  > 定位为风险预警信号，不进入合成。见 `reports/dragon_net_20_review.md`。
- **构建即审查**：因子入库自动执行审查（结构/分布/覆盖/IC/PIT 穿越自检），
  结果存 `data/lake/factor_audit/`，报告 `reports/factor_audit.html`；
  合成层设审查闸门——引用 FAIL 因子直接拒绝（`composite._audit_gate`）
- **前向验证**：每日决策快照入纸面台账（`signal_portfolio`），
  `scripts/forward_monitor.py` 生成前向净值 vs 中证1000（`reports/forward_monitor.html`）；
  容量测算（`scripts/capacity_analysis.py`）：持仓 ADV 中位 ~4900 万/日，
  5000 万资金规模下持仓/ADV 中位仅 1%，容量上限约 3~5 亿元
- 风控：个股权重上限 5%、行业权重上限 30%
- 持仓状态存 `portfolio_state/current.json`，调仓历史存 `state_history.json`

## 并发与锁机制（重要）

DuckDB 是**单写者**模型，本系统的策略：

1. **写任务串行**：`data init` / `data update` 持写锁；若锁冲突立即报错（勿并发跑两个更新）
2. **查询永不阻塞**：`query()` 入口三层降级
   - ① 本进程已持有连接 → 直查
   - ② 只读直连 DuckDB（无写锁时）
   - ③ **Parquet 镜像**（写锁被占时，数据截至最近一次更新；镜像在每次更新完成后自动导出）
3. 查询类命令（`query`/`data status`/`data quality`/`universe`）任何时刻可跑

## 目录结构

```
quant/
├── cli.py                    # CLI 入口（agent 与人类共用）
├── scripts/
│   ├── daily_pipeline.py     # 每日七步流水线（调度入口）
│   ├── api_server.py         # FastAPI 只读研究网关（协作查询/报告服务）
│   ├── init_data.py          # 全量首建（幂等）
│   └── accept_p1.py          # P1 验收脚本
├── quantlab/
│   ├── config.py             # 全局配置（路径/参数/指数表/交易成本）
│   ├── data/
│   │   ├── store.py          # DuckDB 存储引擎 + Parquet 镜像
│   │   ├── calendar.py       # 交易日历
│   │   ├── instruments.py    # 股票主表 + 当日快照
│   │   ├── kline.py          # K线采集 + 复权因子引擎
│   │   ├── financial.py      # 财务快照
│   │   ├── universe.py       # 多股票池
│   │   ├── update.py         # 日度更新调度器
│   │   ├── quality.py        # 数据体检
│   │   ├── cli.py            # data 子命令实现
│   │   ├── sources/          # 数据源适配器（tdx/tencent/sina/eastmoney/ths）
│   │   └── feature/          # 特色数据（龙虎榜/两融/解禁/大宗/股东户数/热点/北向/资金流）
│   └── factor/               # L2 因子层（基类/注册表/内置因子/计算/IC评估）
│       ├── base.py           # Factor 基类 + SqlFactor
│       ├── registry.py       # @register 注册表
│       ├── compute.py        # 计算入口（compute_factor/compute_all）
│       ├── quality.py        # 因子质量（IC/覆盖率）
│       └── library/          # 内置因子库（price/valuation）
│   └── model/                # L3 模型层（多因子合成/选股回测/绩效）
│   └── optimize/             # L4 优化层（权重方案/优化回测）
│   └── decision/             # L5 决策层（信号/调仓/风控/状态）
├── data/
│   ├── quant.duckdb          # 主库
│   └── lake/                 # Parquet 湖（clean 镜像 + factor 因子库 + factor_audit 审查）
└── docs/
    ├── quantlab_guide.html   # 合作版图文讲解材料（新人入口）
    ├── collab_setup.md       # 协作环境搭建与数据分发指南
    ├── service_deploy.md     # 服务化部署指南（API 网关/JupyterLab）
    ├── architecture.html     # 架构设计文档
    └── DEV_LOG.md            # 迭代日志
```

## 已知限制（沙箱网络环境实测）

- 东财 `push2`/`push2his` 不可达 → 个股资金流域 degraded（`probe_available()` 探测恢复后自动启用）
- 腾讯 `ifzq.gtimg.cn` K线接口被 WAF 封 → 指数K线走通达信
- 北向资金数据上游有断供历史 → 建议定期导出 CSV 备份
- iFinD 增强源预留适配器位（`sources/` 下加 ifind_source.py 即可接入）
