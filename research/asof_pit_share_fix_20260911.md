# as-of 缺口根治 + 腾讯市值列错位事故

日期：2026-09-11 深夜 ~ 09-12 凌晨
触发：用户要求「修复 asof 问题」
状态：**已修复**（代码 + 历史数据 + 新数据表 + 审计口径 + 质量防线）

---

## 一、结论速览

| 项 | 结果 |
|---|---|
| as-of 缺口根因 | `finance_snapshot` 单期快照（1 行/股）的股本回填全历史 |
| **根治手段** | 新建 **PIT 逐日股本表 `share_capital_daily`**（1629 万行 / 5510 只 / 2000-01-04~2026-09-10） |
| 数据源 | free-stockdb 日线 21 字段中的 `total_share`/`float_share`（**逐日真值，非回填**） |
| 受影响因子 | 11 个引用 `finance_snapshot` 的因子全部切换为 `ASOF JOIN share_capital_daily` |
| **顺带发现的严重 bug** | `tencent_source.batch_quotes` 把接口 `[44]`(流通市值)/`[45]`(总市值) **写反**，已修正 15038 行历史数据 |
| 次要发现 | ① `ep`/`bp` **同名重复注册**（`valuation.EP/BP` 被 `fundamental.Ep/Bp` 覆盖，前者死代码）② audit 的 as-of 判定只按表名，`op_margin` 属假阳性 |
| 新增防线 | tencent 市值自愈守卫、quality「总市值≥流通市值」硬检查、registry 重名告警 |

---

## 二、修复前的 as-of 缺口

`audit.py::_ASO_TABLES = {finance_snapshot, instruments, index_members}`
——引用即记 WARN。304 份 audit 中 **8 WARN / 296 PASS / 0 FAIL**：
`bp / dragon_net_20 / op_margin / size / sp / total_mcap / turnover / turnover_std_20`

三张 as-of 表实测：

| 表 | 行数 | 时点信息 | 根因 |
|---|---|---|---|
| `finance_snapshot` | 5551 = 5551 只（1 行/股） | report_period 全 = 2026-06-30 | 股本/财务**当前值回填历史** |
| `instruments` | 5460 = 5460 只（1 行/股） | updated_at 同一时刻 | `is_st`/`industry` 为当前状态 |
| `index_members` | 3850（当前成分精确 300+50+500+1000+2000） | `is_current` 全 True、`in_date` 全 NULL | 纯当前成分，零历史版本 |

三表均走 `store.replace_table()`（DROP+CREATE+INSERT）。

### ⚠️ 修正此前研究报告的错误口径

`research/pit_audit_ic03_20260910.md` 第 23 行称 8 个缺口「随财务快照积累缓解」。
**该说法不成立**：`finance_snapshot` 每次流水线整表替换（`financial.py:76`），
永远只有最新一期 1 行/股，不会自然积累历史。缺口会长期存在。

---

## 三、根治方案：PIT 逐日股本表

### 3.1 为什么不用「送转事件反推」

直觉方案是用 `dividend_events`（送转/配股）从当前股本反推历史，**但实测不可行**：

> 平安银行 2015-01 反推得 **190.25 亿股**，真值 **114.25 亿股**，误差 **+40%**。
> 原因：**增发（定增/公开增发）才是股本变动主因**，而它没有事件表。

### 3.2 正确数据源：fsdb 日线的股本字段

`fsdb_source.day_bars()` 返回 21 字段，其中直接含逐日股本：
```
date/code/name/open/high/low/close/pre_close/volume/amount/turnover/pct_chg/
amplitude/is_st/vol_ratio/**total_share**/**float_share**/total_mv/float_mv/pe_ttm/pb
```

**真实性验证（决定性）**：
- 20/20 抽样股票的 `total_share` 呈**阶梯式变化**（非回填的常数）；
- 茅台股本阶梯逐次准确：2006 转增→9.438 亿、2014 送股→11.42 亿、
  **2025-09 回购注销→12.5227 亿**（连回购都能捕捉）；
- 建行 2010-01-04 = 2336.89 亿股（当前 2616 亿）；
- 平安银行 2015-01-05 = 114.25 亿股（当前 194 亿）——与公开历史一致。

### 3.3 表与用法

```sql
share_capital_daily(code, date, total_shares, float_shares, source)  -- PK(code,date)
```

因子侧必须用 ASOF JOIN（取 <= t 最近一条 = PIT 语义，停牌日股本前向保持）：
```sql
FROM kline_daily k
ASOF JOIN share_capital_daily sc ON sc.code = k.code AND k.date >= sc.date
```

**覆盖度**：直接日期命中 **99.94%**；ASOF 命中 **100%**（5,863,467/5,863,467）。

回填：并发 4（fsdb 护栏），5310 只 / 1629 万行 / **4 分 11 秒** / 0 失败。
脚本 `scripts/backfill_share_capital.py`（幂等、断点续拉），已接入流水线 `update.py` 3.8 节。

---

## 四、顺带发现的严重 bug：腾讯市值列错位

### 4.1 现象

`daily_snapshot` 在 2026-09-03/04/07/08/11 出现 **59% 的行「总市值 < 流通市值」**
（非全流通股中 ~90%）——数学上不可能（流通股本 ⊆ 总股本）。

### 4.2 根因（实测接口确认）

腾讯 `qt.gtimg.cn` 字段布局实测（建行/平安/宁德交叉验证）：
```
[43] 振幅   [44] 流通市值   [45] 总市值   [46] PB
```
而 `tencent_source.py` 写的是：
```python
"mcap_yi": _f(44),        # ← 实为流通市值
"float_mcap_yi": _f(45),  # ← 实为总市值
```
**两列完全写反。** 09-11 库里建行 `1057.22 / 28828.36` 与接口原始值逐位吻合，
证明就是该路径写入。fsdb/新浪回补的日期（09-01/09/09/09-10）按字段名取值，故未受影响。

### 4.3 修复

- `tencent_source.py`：改为 `mcap_yi=_f(45)` / `float_mcap_yi=_f(44)`；
- 新增 `_swap_guard()` 运行时自愈：若 >50% 样本违反不变量则自动交换并报错，
  少量异常置 NaN——防止未来接口布局再变时静默污染下游；
- 历史数据：`scripts/fix_snapshot_mcap_swap.py` 备份后逐行修正 **15038 行**，残余违反 0；
- 其它列体检：`turnover_pct` 与由成交量反算的换手率完全自洽（0.46498 vs 0.465），
  **证明只错位了 44/45 这一对**。

**影响面**：`universe.py` 的 `top500/top2000/top3000` 池按 `float_mcap_yi` 排序，
错位期间实为按总市值排序；研究默认池 `ashare_ex` 不受影响。
`prodmodel_capacity.py` 的流通市值容量估算受影响。

---

## 五、口径切换的影响量化

旧口径（`finance_snapshot` 当前股本）vs 新口径（PIT 逐日股本）：

| 因子 | Spearman | 平均 rank 位移（占截面） |
|---|---|---|
| turnover | 0.9056 | 8.0% |
| size | 0.9201 | 7.2% |
| turnover_std_20 | 0.9219 | 7.4% |
| chip_vwap_bias_250 | 0.9464 | 4.9% |
| total_mcap | 0.9799 | 3.3% |
| sp | 0.9862 | 2.8% |
| ocfp | 0.9960 | 1.4% |
| lockup_pressure_60 | 0.9994 | 0.2% |
| dragon_net_20 | 1.0000 | 0.0% |
| cp / bp | 1.0000 | 0.0%（见第六节，实现被覆盖） |

**关键读数**：用 `float_shares` 的因子（size/turnover/turnover_std_20）改动最大
（rank 位移 7~8%）；用 `total_shares` 的因子改动小。原因见下。

### 5.1 两源股本口径差异（重要）

| 股票 | 通达信 总/流通 | fsdb 总/流通 | 腾讯（裁判） |
|---|---|---|---|
| 建设银行 | 2616.004 / 95.937 | 2616.004 / 95.937 | 一致 ✓ |
| 贵州茅台 | 12.501 / 12.501 | 12.501 / 12.501 | 一致 ✓ |
| **民生健康 301507** | 3.566 / **1.102** | 3.566 / **3.562** | **fsdb 对** |
| **艾芬达 301575** | 1.213 / **0.244** | 1.213 / **0.728** | **fsdb 对** |
| **中巨芯 688549** | 14.773 / **5.893** | 14.773 / **14.773** | **fsdb 对** |

**`total_shares` 两源基本一致；`float_shares` 在次新股/解禁股上通达信严重偏低（约 1/3）**。

典型案例：民生健康 2026-09-07 大量限售解禁，流通股本 **1.102 亿 → 3.562 亿**。
通达信快照停留在解禁前，旧口径把解禁后的历史市值低估 3.2 倍。

→ 结论：**新表在「时点」和「口径」两个维度同时优于旧口径**。

---

## 六、次要发现（未彻底修复，已登记）

### 6.1 `ep` / `bp` 同名重复注册

`valuation.EP`（name="ep"）被 `fundamental.Ep`（name="ep"）覆盖，
`valuation.BP` 同理。生效实现是 `fundamental.Ep = 1/pe_ttm`、`Bp = 1/pb`
（**不用股本**，故不受 as-of 影响，重算字节级无变化是正确行为）。
被覆盖的 `valuation.EP/BP` 为死代码。

风险：生效实现取决于 **import 顺序**；且 audit 里 `bp` 的 as-of 记录挂在
已被覆盖的实现上（陈旧）。已在 `registry.register()` 加显式告警 + `shadowed()` 查询。

### 6.2 audit 的 as-of 判定过粗（已修）

原 `_ASO_TABLES` 只按**表名**判定 → `op_margin`（只用 `operating_profit/revenue`，
不碰股本）被误判 as-of。已改为**按列判定**（`_asof_caveat()`）：只有引用
`total_shares/float_shares/total_share/float_share`（instruments 另含 `is_st/industry`）
才记缺口。

### 6.3 未覆盖的 as-of 面

`instruments`（`is_st`/`industry` 为当前状态）与 `index_members`（纯当前成分）
**仍无 PIT 版本**，但目前无因子引用，属潜在雷区。
`index_members_hist` 为月度市值排名法近似成分，精度有限（声明不用于精确归因）。

---

## 七、复算入口（幂等可重跑）

```bash
# 全量回填 PIT 股本（幂等，可中断续拉）
python scripts/backfill_share_capital.py

# 修正 daily_snapshot 历史市值列错位（自动备份）
python scripts/fix_snapshot_mcap_swap.py

# 重算受影响因子
python cli.py factor compute size    # total_mcap/turnover/turnover_std_20/sp/ocfp/
                                     # chip_vwap_bias_250/dragon_net_20/lockup_pressure_60

# 重跑审计
python scripts/factor_audit.py --names size,total_mcap,turnover,turnover_std_20,sp,ocfp,chip_vwap_bias_250,dragon_net_20,lockup_pressure_60,bp,op_margin --force

# 诊断/验证
python scripts/diag_snapshot_swap.py            # 市值列错位定位
python scripts/diag_share_source_crosscheck.py  # 四方股本口径交叉验证
python scripts/diag_pit_share_impact.py         # 新旧口径影响量化
python scripts/compare_factor_before_after.py   # 因子前后对比
```

**回滚**：旧值备份在 `data/backup_factors_20260911/`，
`daily_snapshot` 旧表备份在 `data/lake/clean/mirror/daily_snapshot_backup_20260911.parquet`。

---

## 八、对生产模型的提示

`size` 是生产模型（size + amihud_20 等权 top100）的因子之一，
本次切换使其 rank 位移 7.2%（Spearman 0.9201）。
**生产模型的持仓名单与历史回测指标需重新评估**——
候选账本的双闸门（2026-12）应基于新口径重算。

---

## 九、重建回归：8 个 PIT 审计 FAIL 及其根治（09-12 凌晨）

### 9.1 现象

切换 PIT 股本后，`scripts/factor_audit.py --names ... --force` 出现
**FAIL 8 / PASS 2 / WARN 1**，8 个 FAIL 全部是
`PIT 穿越自检 FAIL：截断重算与全量不一致（使用了未来数据）`。

样本：`{"code": "688808", "full": 5.28032801, "cut": 0.0}`
—— 全量版有值，截断版为 0，看似穿越，实则**假阳性**。

### 9.2 根因：`append_factor` 的 `keep="last"` 语义

`store.append_factor()` 做的是按 `(code, datetime)` 合并去重、`keep="last"`
——**本次计算未产出的行会保留旧值**。

`688808` 等 64 只股票在 `share_capital_daily` 中**无数据**
（fsdb 未收录其逐日股本），新的因子 SQL 有 `WHERE f.float_shares > 0`，
于是新计算不产出这些股票 → 但 parquet 里残留着上一版（`finance_snapshot` 口径）
写入的旧行 → PIT 截断重算时旧行被时间窗滤掉，与全量版不一致 → 判 FAIL。

**影响面量化**：64 只、全部为 2026-04-24 ~ 2026-09-07 新上市股、共 2822 行。
`ashare_ex` 池本就剔除上市 < 140 日的次新股，故这些标的本就在研究/生产池之外。

### 9.3 根治

新增「整年重写」能力，消除旧行残留：

| 层 | 改动 |
|---|---|
| `quantlab/data/store.py` | `append_factor(factor_name, df, replace: bool = False)` —— `replace=True` 时按年整文件重写 |
| `quantlab/factor/compute.py` | `compute_factor(..., rebuild: bool = False)` 透传 |
| `cli.py` | `factor compute <name> --rebuild` |

**决策**：不用 `finance_snapshot` 兜底这 64 只（会重新引入 as-of 缺口），
接受新上市股缺口 —— 反正它们不在可交易池内。

### 9.4 重建结果（11/11 成功）

| 因子 | 行数 | 只数 | 区间 |
|---|---:|---:|---|
| size / total_mcap / turnover / ocfp / chip_vwap_bias_250 / lockup_pressure_60 | 5,863,467 | 5492 | 2022-01-04~2026-09-10 |
| turnover_std_20 | 5,857,975 | 5492 | 2022-01-05~2026-09-10 |
| sp | 5,863,059 | 5492 | 2022-01-04~2026-09-10 |
| dragon_net_20 | 5,863,467 | 5492 | 2022-01-05~2026-09-11 |
| ep / bp | 6,849,897 / 6,906,652 | 5507 | 2021-01-04~2026-09-10 |

校验：所有因子中 `688808` 残留行数 = **0**。

### 9.5 附带补齐：`share_capital_daily` 的 Parquet 镜像

`share_capital_daily` 加进了 `Store.MIRROR_TABLES`，但镜像导出只在日更新末尾触发，
本次新增表**尚未导出** → `Store.query()` 的第三层降级（写锁被占时的无锁兜底）
会因缺镜像而失败。已手动 `Store().export_mirror()` 补齐。

### 9.6 环境提示：主库并发争用

本机同时存在其他工作负载（`scripts/fundamental_ab_test2.py`、
`scripts/backfill_minute_xiaodefa.py`）占用 `quant.duckdb` 写锁，
批量重算会被 `IO Error: Cannot open file ... 另一个程序正在使用此文件` 打断。
`scripts/post_rebuild_20260912.py` 实现了「轮询等锁再执行」的编排，可复用：

```bash
python scripts/post_rebuild_20260912.py --wait-sec 1500
```

### 9.7 重建+重审最终结论

`scripts/factor_audit.py` 强制重审 12 个因子：**PASS 11 / WARN 1 / FAIL 0**
（此前 FAIL 8）。唯一 WARN 为 `op_margin`「截面覆盖中位数仅 29 只」——
通达信快照缺营业利润科目的既有数据边界，非本次回归。

`size / total_mcap / turnover` 的 PIT 记录已从
`asof_caveat=[finance_snapshot]` 变为 **`asof_caveat=[]`**，
且 `truncated` 正确包含 `share_capital_daily`（`WHERE date <= DATE 't0'`）。

---

## 十、审计自身的两个缺陷（2026-09-12 修复）

### 10.1 「假 PASS」：SKIP 被末尾赋值覆盖

`pit_audit()` 原逻辑：

```python
for t0 in t0s:
    cut_sql = _build_cut_env(...)
    if cut_sql is None:
        out["status"] = "SKIP"        # ← 循环内设置
        continue
    ...
out["status"] = "FAIL" if total_diff > 0 else "PASS"   # ← 无条件覆盖！
```

**后果**：凡是「SQL 未引用可截断表」的因子（三个 t0 全跳过、`total_diff` 恒 0），
最终状态被写成 **PASS**。这不是「通过」，而是「从未检验」。

修复：加 `n_tested` 计数，`n_tested == 0 → SKIP`；
并在 verdict 判定里把 `SKIP` 显式记为 WARN
（`"PIT 未检验（SQL 未引用可截断表，如 parquet 直读）"`），
避免「未检验」被读成「已通过」。

### 10.2 parquet 直读数据集的 PIT 盲区（20 个因子）

105 个 registry 因子里有 **20 个**用 `read_parquet('...')` 直读湖文件，
SQL 中不含表名，`_build_cut_env` 直接返回 None → 长期假 PASS：

```
accruals2 bp cfp_ttm ep ep_z5 garp gpoa gpoa_chg np_q_yoy ocf_to_profit
op_margin rev_q_yoy roe_chg roe_cut roe_ttm roic sp_ttm sue_gpoa sue_v2 sur
```

修复：`_build_cut_env` 增加 parquet 数据集识别与物化——

| 数据集 | 时间列 | 截断口径 |
|---|---|---|
| `…/fundamental/valuation_daily/part-*.parquet` | `date` | `WHERE date <= t0` |
| `…/fundamental/finance_q/{income,balance,cashflow}/part-*.parquet` | `InfoPublDate` | `WHERE InfoPublDate <= t0` |

做法：把 `read_parquet('path'[, args])` 调用物化为 `audit_mem.<名>` 截断副本，
再把 SQL 中的调用文本整体替换为表名（同一数据集多次引用也全部替换）。
若某数据集**无任何可用时间列**，则该因子不进入假 PASS，而是如实返回 SKIP。

重审脚本：`python scripts/reaudit_parquet_pit.py`（自带等锁）。

**重审结果（20 个因子，全部从「假 PASS」变成真实检验）**：

| 结论 | 数量 | 因子 |
|---|---:|---|
| PASS（真实截断检验，n_diff=0） | 18 | accruals2 bp cfp_ttm ep ep_z5 garp gpoa np_q_yoy ocf_to_profit op_margin rev_q_yoy roe_chg roe_cut roe_ttm roic sp_ttm sue_v2 sur |
| **FAIL（暴露真 bug）** | 2 | `gpoa_chg` `sue_gpoa` |

截断来源正确登记，例如 `ep` → `valuation_daily WHERE date <= t0`、
`roe_ttm` → `finance_q_income WHERE InfoPublDate <= t0`。

### 10.3 新发现的穿越缺陷：`gpoa_chg` / `sue_gpoa`

**现象**：两者都在 t0=2024-05-14 各差 **1 只**（300995），另两个 t0 无差异。

**根因：按「行数」取滞后，对缺期敏感。**
`300995` 的 **2023 年报被延迟到 2025-04-15 才披露**（同期还有 2024 年报同日）。
该行在全量数据中位于 2023-09-30 与 2024-03-31 之间，使行序窗口整体位移：

| 版本 | 计算 2024-03-31 的 4 期滞后 | 月差 | 结果 |
|---|---|---|---|
| 全量（含 2023-12-31 行） | 前 4 行 = 2023-03-31 | 12 ✓ | 产出值 0.0195 |
| PIT 截断（截至 2024-05-14） | 前 4 行 = 2022-12-31 | 15 ✗ | 无值（正确） |

即全量版**隐含使用了「未来会存在这一期报表」的信息**才让月差恰好等于 12。
这是真穿越，但量级极小（1/5150 只 ≈ 0.02% 截面，仅 1 个 t0）。

**建议修法**（待做，属因子逻辑变更 → 需走 WF 复验）：
把 `LAG(rp, 4)` / `ROWS BETWEEN 8 PRECEDING` 改为**按报告期自连接**
（`rp - INTERVAL 12 MONTH` 或按 `rp` 严格对齐），把「第几行」换成「哪一期」。

**当前处置**：保持 FAIL 不动（`build_composite` 的 `_audit_gate` 会自动拒绝其进入合成）。
问题描述已加上不一致规模（`最大不一致 N 只 = x.xxx% 截面`），便于判断严重度。

### 10.4 全库最终状态

| 指标 | 修复前 | 现在 |
|---|---:|---:|
| 审查记录总数 | 311 | 311 |
| PASS | 296（含 20 个假 PASS） | **309** |
| WARN | 8 | **0** |
| FAIL | 0（掩盖） | **2**（真实暴露） |
| as-of caveat | 8 | **0** |

> 「FAIL 从 0 变成 2」不是退步——是审计终于能看见原本看不见的东西。

---

## 十一、`share_capital_daily` 股本冲突（total < float）

硬不变量「总股本 ≥ 流通股本」在 1629 万行里有 **8881 行**违反。
2026-09-12 逐类拆解（**大多是噪声，不是数据错**）：

| 类别 | 行数 | 只数 | 处置 |
|---|---:|---:|---|
| 浮点噪声（相对差 ~1e-7，如 601166 报 211.628519 亿 vs 211.628552 亿） | ~3300 | 45+ | 加 `1e-6` 相对容差即可 |
| 北交所（92/83/87/43 开头，两字段疑语义互换，如 920009 total 1008 万 / float 7754 万） | 2153 | 305 | **池外**（`ashare_ex`/`ashare_main` 已剔除）→ WARN |
| **上游字段滞后**（实质错误） | 439 | 19 | 需修 |
| 其中：`600372` 中航电子 2023-04-19~07-14，float 是 total 的 **2.34 倍** | 58 | 1 | 需修 |

**600372 案例的时间线**（说明哪个字段可信）：
换股吸收合并中航机电的新股于 2023-04-19 上市 → `float_share` 当天跳到
44.85 亿（= 合并后总股本），而 `total_share` 仍停在合并前的 19.18 亿，
直到 2023-07-17 配套融资落地才更新为 48.39 亿。
→ **float 字段及时，total 字段滞后**；窗口期内 EP/BP/SP/OCFP 的市值分母被低估。

处置：
1. `quality.py` 第 8 项检查改为 **1e-6 相对容差 + 北交所单列**，
   非北交所违反才判 FAIL；
2. `scripts/fix_share_capital_conflict.py` 提供可回滚的修复
   （备份为 parquet、`total_shares = float_shares`、`source` 标记 `fsdb+clamp`）。

### 11.1 与既有 fsdb 估值表的交叉验证（强证据）

湖里本来就有一张 fsdb 逐日估值表
`data/lake/clean/fundamental/valuation_daily/part-*.parquet`
（690.8 万行 / 5507 只 / 2021-01-04 起，字段含 `total_share`/`float_share`/`pe_ttm`/`pb`），
是**另一条独立构建路径**的产物。用它反查本次新建的 `share_capital_daily`：

| 对比项 | 重叠行数 | 不一致行数 |
|---|---:|---:|
| 总股本 `total_shares` vs `total_share` | 6,908,164 | **0**（0.0000%） |
| 流通股本 `float_shares` vs `float_share` | 6,908,164 | **0**（0.0000%） |

→ 两个独立通路（HTTP 逐只拉 `day_bars` vs 既有批量管线）字节级一致，
`share_capital_daily` 的值可信。

**顺带结论**：`valuation_daily` 起点 2021、且是 parquet 直读（不能进因子 SQL 的
表名重写）；`share_capital_daily` 起点 2000、是主库表，故后者更适合承担
「PIT 股本」这一职责。两者不重复建设，前者继续承担估值字段（pe_ttm/pb）。

