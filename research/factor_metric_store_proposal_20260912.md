# 因子评估指标日频存储方案（提案 + 外部依据 + 合理性裁决）

日期：2026-09-12 ｜ 状态：提案（待实施）｜ v2：新增股票池维度（§3.6，每池一库 + 成员 PIT 硬闸门）

## 0. 结论先行

**方案方向合理，且是业界标准做法；但原始提案需要做三点修正：**

1. **ICIR/RankICIR 不是"每日一条的原子指标"，它是窗口统计量**——只要存了日频 IC 序列，任意窗口的 ICIR/胜率/t 值都可派生。每日硬存 5 个 horizon × 2 类型的 ICIR 是冗余存储且口径容易和"评估时按需算"打架。正确分层：日 IC 入原子层，rolling ICIR 作为可重建的派生缓存（建议预存 w=20/60/120 三档加速监控查询）。
2. **h 日 IC 在信号日 t 不可计算，必须等 t+h 收益落地**——采用业界标准的"按信号日记账、成熟后回填"机制：每日流水线为 T-1/T-5/T-20/T-60/T-120 五个信号日各补一格。这天然无前视、时间轴对齐，且覆盖写 parquet 与项目 safe-delete 约束（写入不删、同名覆盖=幂等）完全兼容。
3. **"与其他因子的相关系数"全矩阵每日存是浪费**——312 因子全对全 = 每日 48,516 对，且每日全量计算成本高。降级为：每日只算 vs 核心 ~10 因子（6 风格代表 + 生产/候选成分），全 N×N 矩阵每周一次。

4. **（v2 新增）股票池维度合理，按池分库存储**——同一套指标在不同 universe（红利成分股、指数成分、自定义池）内重算，是因子"池内有效性"研究的标准需求（本项目红利池 M2 结论正是池内 size+amihud top10）。**硬性前提：池成员必须 PIT（按信号日取当日成分），否则就是已记录在案的"当前池回填"幸存者偏差**。设计见 §3.6。

此外，用户问"还有什么需要每天存的"——按 Alphalens 标准指标族 + 信号监控实践，建议补充：**覆盖率、截面分布、rank 自相关（换手率代理）、Q1/Q5 组换手、多空实现收益、风格暴露**。清单见 §4。

## 1. 现状盘点：三层快照，缺时间序列层

现有指标存储全部是"最新快照"式，没有任何日频时序：

| 层 | 位置 | 内容 | 缺陷 |
|---|---|---|---|
| 审计快照 | `data/lake/factor_audit/*.json`（311 个） | 全史聚合 IC/ICIR/胜率（rank+pearson × h=1/5/20）、覆盖、分布、PIT | 单点全史值，看不到衰减过程；刷新串行 ~40 分钟 |
| FDR 批表 | `reports/fdr_factor_ranking.json` | t_adj / q / HLZ | 库级批量，重算才更新 |
| 评估索引 | `reports/factor_eval/index.json` | 报告红绿灯 | 事件式，非连续 |

**缺口**：因子"什么时候开始衰减/失效"在现有体系里不可见——每次评估都从零重算全史，无法回答"最近 60 天 IC 相比历史均值掉了几个 σ"这类监控问题。本提案补齐的正是这一层。

## 2. 外部依据（2026-09-12 检索）

### 2.1 Alphalens（Quantopian，业界因子评估事实标准）

- 核心抽象就是**日频原子序列**：`factor_information_coefficient()` 输出逐日 IC Series；`mean_information_coefficient(by_time=...)` 再从日序列按月/任意窗口聚合。**窗口统计一律派生，不预存**——印证修正点 1。
- `mean_return_by_quantile(by_date=True)`：逐日分位组收益序列；`compute_mean_returns_spread()`：顶底组价差——印证 Q1-Q5 日频存储的必要性。
- `factor_rank_autocorrelation()` + `quantile_turnover()`：因子秩自相关与分位组换手是标准日频指标——本项目当前完全没有，建议补入。
- 默认周期组 `(1, 5, 10)`，教程用 `1/5/10`；长周期评估自行扩展——本项目 20 日调仓口径 + 60/120 慢周期谱与既有看板一致，horizon 取 1/5/20/60/120 合理。

### 2.2 业界因子中间库 / 生命周期管理实践

- 因子中间库设计（量化投顾高性能因子中间库）：生产因子"晋升后进入每日例行计算 + 持续监控实盘 IC 变化 + 衰减预警（近期 IC 均值持续低于历史均值或符号反转即触发）+ 归档不删除"。——印证"日频入库 + 监控预警"的生命周期闭环。
- 多因子轮动实盘监控框架：逐日 append 因子 IC 序列，滚动窗口（252）算 IC 均值/胜率做健康判定与因子更替。——与本提案 L0→预警的消费路径同构。

### 2.3 信号健康监控（signal-monitor，开源生产实践）

- rolling IC + Fisher 变换置信区间（CI = tanh(atanh(IC) ± 1.96/√(n-3))）——日频 IC 序列上直接可算，建议作为预警口径。
- 拥挤/失效先行指标：信号-成交量相关、IC 自相关衰减、Bayesian changepoint 检测、OU 平稳性（半衰期>100 日则历史 IC 无预测力）——这些全部**只需要日频 IC 序列 + 因子日值截面**即可计算，是本存储方案的低成本增值消费方。

**裁决**：提案与 Alphalens 的原子序列抽象、业界因子生命周期管理、生产信号监控实践三者一致，方向正确。

## 3. 修正后的分层设计

```
L0 原子时序层   factor_metric_daily     每日必存 · 不可派生 · source of truth
L1 派生缓存层   factor_metric_rolling   每日刷 · 随时可从 L0 重建
L2 相关性层     factor_corr_daily/weekly 每日核心对 + 每周全矩阵
L3 按需计算层   （不预存）               run_eval / build_dashboard 消费 L0 加速
```

### 3.1 L0 `factor_metric_daily`（宽表，主键 (date, factor)）

存储：`data/lake/factor_metric_daily/part-{year}.parquet`（按年单文件，覆盖写幂等；**不进 DuckDB 主库**，绕开单写者写锁，符合 Parquet=source of truth）。

| 组 | 列 | 说明 |
|---|---|---|
| 键 | `date, factor` | date=**信号日**（见 §3.4 成熟回填） |
| IC 原子 | `rank_ic{1,5,20,60,120}` `pearson_ic{1,5,20,60,120}` | 与审计同口径（ashare_ex 全截面）；pearson/rank 双类型同 v2 审计 schema |
| 分组收益 | `q1_ret/q2_ret/q3_ret/q4_ret/q5_ret/ls_ret`（h=1 日频口径）+ `q1_ret20..q5_ret20/ls_ret20`（h=20 调仓口径） | 用 daily_segments 自带收益字段（项目硬口径）；ls = Q5-Q1 |
| 覆盖 | `n_codes, coverage` | 数据质量闸门，跌穿阈值预警 |
| 分布 | `f_std, f_q01, f_q50, f_q99` | 分布漂移监控（审计全史值 ↔ 日频序列） |
| 稳定性 | `rank_ac1, rank_ac5, rank_ac20` | factor rank autocorrelation（Alphalens 标配，换手/成本代理） |
| 换手 | `q1_turnover, q5_turnover` | 顶底组换入比例，直接喂交易成本模型 |
| 暴露 | `exp_size, exp_bp, exp_mom20, exp_vol20, exp_turnover, exp_rev5` | 对 6 风格代表当日截面 spearman（看板已有此计算，日频入库后可监控暴露漂移） |

每日新增 312 行；年增 ~7.8 万行 × ~40 float 列 ≈ **每年 <10MB**。全历史 2022 起 ~1136 交易日 × 312 ≈ 35 万行 < 40MB。**容量完全不是约束，设计重心在口径正确性。**

### 3.2 L1 `factor_metric_rolling`（派生缓存，主键 (date, factor)）

- 列：`rank_icir{h}_w{w}` / `rank_win{h}_w{w}` / `rank_t{h}_w{w}`，h∈{5,20}（主口径），w∈{20,60,120}
- 全部可从 L0 的日 IC 序列 groupby-rolling 秒级重建——**定位是监控看板的查询加速缓存，不是事实源**；任何窗口不同的 ICIR 需求走 L3 按需算。
- 可与 L0 合并为同一张宽表的附加列（同分区同键，更新成本低），物理分表不是必须。

### 3.3 L2 相关性层（长表）

- `factor_corr_daily`：`(date, factor, core_factor, rho)`，每日 312 × ~10 ≈ 3,120 行，截面 spearman（对秩）。
- `factor_corr_weekly`：`(week, factor_a, factor_b, rho)`，全矩阵 48,516 对/周，年 ~250 万行长表 ~20MB。
- 全矩阵每日算的隐性成本不只是存储：每日要 join 312 个因子截面做 4.8 万次 corr，而 99% 的配对日变化无信息量；周频足够捕捉冗余结构迁移（看板拥挤度扫描当前用 60 日窗，周频完全匹配）。

### 3.4 成熟回填机制（最关键的正确性设计）

h 日 IC 需要 t→t+h 收益，信号日 t 当日不可知。每日流水线 T 日运行时：

- 为信号日 T-1 补写 `*_ic1` 与 h=1 分组收益；
- 为 T-5 补 `*_ic5`；T-20 补 `*_ic20` 与 h=20 分组收益；T-60/T-120 同理。

**行按信号日记账**：IC 序列时间轴与因子信号天然对齐，画图/滚动窗口不需要 shift；每行的列随成熟逐步填满（未成熟列为 NULL）。回填=重算该行并重写 `part-{year}.parquet`——同名覆盖幂等，兼容 safe-delete"写入一律不删"约束，也天然兼容 append_factor keep="last" 的教训（这里是整行重算重写，无旧行残留问题）。

对照组（不推荐）：按计算日 T 记账——序列错位、每列要单独 shift 才能对齐、容易在未来函数上栽跟头。

### 3.5 不建议每日存的指标

| 指标 | 原因 | 去处 |
|---|---|---|
| FDR q / t_adj | 库级多重检验统计量，批量重算才有意义 | `fdr_multitest.py` 批表 |
| 正交化残差 IC | 逐日截面 OLS 贵且慢变；评估/周频足够 | run_eval / 周任务 |
| 半衰期 / IC 谱形态 | 慢变量 | run_eval |
| 月度 IC 热力 | 由 L0 日序列 groupby 月派生，秒级 | 看板消费 L0 |
| 回测/A-B/TC/盈亏平衡 | 组合层，非因子日频量 | candidate_ab 管道 |

### 3.6 股票池维度（v2 新增）：每池一库，成员必须 PIT

**动机**：池内有效性是全市场有效性的正交问题——红利池 M2（池内 size+amihud top10 净超额 +17.7%/年）这类结论，需要"因子值在池内截面"的 IC/分组收益序列才能日常监控，而不是每次研究时临时重算。

**存储布局（采纳"每池一库"直觉，物理分库 + 逻辑可查全量）**：

```
data/lake/factor_metric_daily/
  ashare_ex/part-{year}.parquet        ← 全市场（= 原设计，默认池）
  dividend/part-{year}.parquet         ← 红利池
  csi1000/part-{year}.parquet          ← 指数成分池（index_members_hist）
  {pool_name}/part-{year}.parquet      ← 任意自定义池，注册即入库
```

- 目录名就是池名（**不用 `pool=` hive 键**，避开"分区名撞列名"的 pandas/DuckDB 坑）；行内仍落 `universe` 列，DuckDB glob `factor_metric_daily/*/part-*.parquet` 可跨池对比查询。
- schema 与 §3.1 完全相同，只是 IC/分组/换手/暴露全部在**池内截面**重排重算；`n_codes`/`coverage` 的分母变为当日池成员数。
- **不加 `pool=` 列而靠目录**：同池跨年单文件覆盖写=幂等，与 safe-delete 约束一致。

**池注册表（新增，PIT 是硬闸门）**：

`data/lake/factor_metric_daily/_pools.json`（或主库小表），每池登记：

| 字段 | 说明 |
|---|---|
| `name` | 池标识（=目录名） |
| `membership_source` | 成员来源：`universe_daily` / `index_members_hist` / 自定义成员历史表 |
| `pit_required` | 恒 true；**按信号日取当日成分**（ join 条件 `member_date <= signal_date` 取最新） |
| `min_codes` | 池内截面最小股票数（默认 30；低于则不计算当日指标，行落 NULL + `degraded=true`） |
| `note` | 口径附注 |

**无 PIT 成员历史的池禁入日频入库**——这是红线：红利池若用"当前成分回填历史"，会把已剔除股的历史弱表现洗出样本，IC 与分组收益系统性虚高（红利池回测的幸存者 caveat 已在案，日频指标层不能重蹈）。红利池须先补 PIT 成员历史（universe_daily 2021 起已重建，红利成分可用 index_members_hist 或自建红利指数成员变迁表），补齐前 dividend 库只从有 PIT 成员的日期起算。

**各指标在池内的口径注意**：

- **IC/rank IC**：池内截面 rank——池越小噪声越大，红利池（~100-300 只）日 IC 波动大，看板须按 rolling 60 看，单日值不作决策。
- **Q1-Q5**：小池每组可能只有 20-60 只，保留五分位但落 `q1_n..q5_n`；池 <150 只时建议同时落 top10/bottom10 收益（对齐红利 M2 的 top10 口径）。
- **风格暴露**：池内暴露仍按池内截面 spearman；注意红利池本身 size/bp 暴露有结构性偏移，解读时对照全市场值。
- **相关性层 L2 不复制到池维度**：因子间相关结构对池选择不敏感，全市场口径足够，池内相关按需算。

**成本**：每加一池，L0 行数 ×1（每池每年 <10MB），日增量计算 ≈ 一次截面 filter + rank，秒级；回填按池独立跑，互不惊动。

## 4. 落地步骤

1. **池注册表 + 红利池 PIT 成员补齐**：落 `_pools.json`；先核实红利池 PIT 成员历史可得性（index_members_hist 或自建变迁表），不可得则 dividend 库起点=有 PIT 成员之日并在 note 声明。
2. **回填脚本** `scripts/factor_eval/backfill_metric_daily.py --pool <name>`：复用 run_eval.py 的 IC/分组计算函数（保证口径同源），2022-01 起全历史，按因子分片 4 进程并行（串行估计 1.5~3h/池；注意 DuckDB 只读、不碰主库写锁）。池维度作为过滤条件注入同一套 SQL，不为池写分支逻辑。
3. **流水线挂钩**：六步流水线"因子计算"后追加 metric 步骤——每日增量 = 各注册池 ×（五个成熟信号日的补写 + 当日覆盖/分布/换手/暴露）+ L1 滚动刷新 + L2 核心对相关（L2 只算全市场）。
4. **周任务**：全矩阵相关 + 可选正交化残差 IC 周频入库。
5. **消费方**：①监控看板（docs/ 新页，全库红绿灯时序，池切换器）；②失效预警（rolling 60 日 rank IC 的 Fisher z-score、连续反号、覆盖率/暴露突变——signal-monitor 口径）；③batch_metrics.py 加第四数据源（近 60 日动态 vs 全史快照对照，支持 --pool）。
6. **验收**：随机抽 3 因子 × 3 窗口 × 2 池，L0 日序列聚合的 IC/ICIR 与 run_eval 现场重算一致（容差 1e-6）；回填后跑一遍 leak-check 式抽查确认无前视（T 日写入的行信号日均 ≤ T-1，且池成员 join 日期 ≤ 信号日）。

## 5. 风险与注意

- **口径漂移**：L0 必须与审计/run_eval 同一套 universe 与收益定义（ashare_ex、daily_segments 收益字段）；在表内落 `audit_version` 或列级注释，改口径时整体 rebuild（keep="last" 教训）。
- **新因子入库**：注册日之前的 L0 历史为空属正常，回填脚本按需补；覆盖率列可区分"未上市/未注册"。
- **基本面因子**：监控口径建议存原始值 IC（与审计同口径）；size 中性化版本若需监控，加 `*_sizeneut` 后缀列，勿混用。
- **ETF/北交所分钟类坑不影响本层**：本层全部基于日频因子湖 + 日收益，无分钟依赖。
- **池成员 PIT 是红线**：任何池在缺少 PIT 成员历史的区间禁止回填指标；宁缺毋假（红利池"当前池回填"幸存者 caveat 已在案）。
- **小池统计噪声**：池 <150 只时 Q1-Q5 与单日 IC 噪声大，消费侧一律 rolling 60 起看；`min_codes` 闸门防"3 只股票算出 IC=1"的假信号。
- **池数量克制**：每池都是一份要维护的口径承诺，注册新池需有明确研究/生产消费方；预计常态池 ≤5 个（ashare_ex + dividend + 1-2 指数池 + 实验池）。
