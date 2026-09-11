# ETF 数据补齐实施记录（2026-09-12）

> 用户要求：补齐剩余 ETF 数据，并探索是否还有其他 ETF 数据，一并实现。
> 起点：`etf_daily` 仅有日线（2,303,259 行 / 2,053 只 / 2004-03-22 起），
> 分钟 / 净值 / 复权 / 持仓四类全缺。

---

## 一、可得性探测（`scripts/probe_etf_sources.py`）

### 1.1 fsdb 侧：**没有 ETF 专用表**

对 510300 逐个发 `?cmd=keys|vals&t={表名}&k1=key:510300` 扫描候选表名：

| 表名 | 结果 |
|---|---|
| `日k` | 3,475 行 ✅ |
| `分钟k` | 98,787 行 ✅ |
| `复权` | 14 条 ✅ |
| `板块` / `股票代码` | 0（key 形式不符，非空表） |
| `ETF` `基金` `净值` `份额` `规模` `成分` `持仓` `IOPV` `折溢价` `指数` `日历` `tick` `5/15/30/60分钟k` `周k` `月k` `ETF成分` `基金持仓` `股票` `证券` `权息` | **全部 0** |

→ **fsdb 不提供 ETF 净值 / 份额 / 规模 / 成分 / 持仓**，这些必须走外部源。

### 1.2 三类可行数据

| 数据 | 源 | 实测深度 | 形态 |
|---|---|---|---|
| **分钟** | fsdb `分钟k` | **2025-01-02 起**（2024 全年 0 根，与股票分钟同边界） | 区间查询，可全量回补 |
| **复权** | fsdb `复权` | 全史；**仅 18.5% 标的有事件**（随机 120 只中 20 只） | 事件表，一次性 |
| **净值** | westock `etf nav` | **上市首日**（510300 → 2012-05-28） | 区间查询，可全量回补 |
| **持仓** | westock `etf holdings` | **仅当日**（`--date` 不生效） | **快照型，只能前向积累** |

细节：
- **ETF 复权 = 年度分红**：510300 有 14 条（2012-12-18 ~ 2026-01-19），
  `div` 0.033~0.123、`give/trans` 恒 0、`cum` 累计到 1.269。
  → 不分红的 ETF 本来就是 0 事件，**空 ≠ 缺数据**。
- **ETF 分钟根数不固定**：2025-01-02 与 2026-01-05 均 **240 根**，
  2026-09-10 为 **242 根**（2026 年网格切换后多出 14:59 零成交 bar + 15:00 收盘 bar）
  → 下游勿硬编码根数。
- **westock `etf profile` / `etf overview` 报 `service error`**（不可用）。

---

## 二、落地实现

四个新模块 + 统一入口，全部接入每日流水线。

| 模块 | 湖路径 | 字段 |
|---|---|---|
| `etf_minute.py` | `clean/kline_1min_etf/part-{code}.parquet`（**按 code 平铺**） | code, datetime, open, high, low, close, volume, amount |
| `etf_nav.py` | `clean/etf_nav/year=YYYY/part-*.parquet` | date, code, nav, close_price, nav_change, nav_change_pct, **premium_pct** |
| `etf_adj.py` | `clean/etf_adj/part-full.parquet` | code, ex_date, div, give, trans, mult, cum |
| `etf_holding.py` | `clean/etf_holding/snap=YYYY-MM-DD/part-full.parquet` | snap, etf_code, kind(top\|pcf), stock_code, stock_name, ratio, rate, change |

- **分钟与股票分钟分目录隔离**（`kline_1min` 是 5,471 只股票的池，混入会污染）
- **`premium_pct = close_price / nav - 1`** —— 折溢价率，湖里此前完全没有的指标
  （实测 510300 全史 std 0.65%、极值 −7.4% ~ +13.3%）
- **持仓含两类**：`top`（重仓股 10 条，带 rate/change）+ `pcf`（申赎清单 10 条）
  ⚠️ PCF 只给前 10 条（源文案写明"共 20 条，显示前 10"），**不是完整清单**

统一回补入口：
```bash
python scripts/backfill_etf_dataset.py --what all        # 四类全补
python scripts/backfill_etf_dataset.py --what minute --start 20250102
python scripts/backfill_etf_dataset.py --what nav --start 20120101
```

流水线（`update.py`）新增三域：`3.10 etf_minute` / `3.11 etf_nav` / `3.12 etf_holding`，
含数据集注册与水位登记（**水位直扫 parquet 取实际落库日期，不取请求日**）。

---

## 三、三个踩坑（均已加防护）

### 3.1 🔴 safe-delete 拦截器阻断写入（最隐蔽）

本机 safe-delete shim 会拦截**批量删除**（>50 个文件 fail-closed）：

```
[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":91,"threshold":50,...}
```

**后果**：ETF 持仓全量快照跑满 **10.5 分钟**，最后一步
`for f in dir.glob('*.parquet'): f.unlink()` 被拦 → **全量数据没写成，
湖里只剩早先小样的 5 只**，而进程返回值看起来是"成功"。

**修法**：写入路径一律不做删除，用**同名覆盖 = 幂等**：
- 固定文件名（`part-full.parquet` / `part-0.parquet`）→ `to_parquet` 直接覆盖
- 重分区 → `existing_data_behavior="overwrite_or_ignore"`，**不 `rmtree` 目标目录**
- 临时目录 → **只读本次 `codes` 对应的文件**（防上次残留污染），清理降级为"尽力而为"

### 3.2 🔴 westock `etf nav` 单次上限 ~1211 行，超限静默截断

实测 `--start 2004-01-01 --end 2026-09-10` → 只回 **1211 行、起点 2021-09-10**，
**不报错**（与 tushare 代理 8000 行陷阱同族）。

| 请求区间 | 返回行数 | 实际起点 |
|---|---|---|
| 2004-01-01 ~ 2026-09-10 | 1211 | **2021-09-10**（截断） |
| 2016-01-01 ~ 2026-09-10 | 1211 | **2021-09-10**（截断） |
| 2021-01-01 ~ 2026-09-10 | 1211 | 2021-09-10（恰好未超） |
| 2012-01-01 ~ 2016-12-31 | 1121 / 1214 | 完整 |

**修法**：按 **3 年切段**（≈730 行，留足余量）+ "行数逼近上限即告警"。
修复后 510300 回到 2012-05-28（3,475 行）、159915 回到 2011-12-09（3,583 行），
**与 fsdb 日线行数完全一致**（可作交叉校验）。

### 3.3 🔴 单只查询无 `**code**` 分段头

westock 的 Markdown 输出：
- **批量**：`**sh510300**` + 表格，接着 `**sh510500**` + 表格
- **单只**：**直接给裸表格，没有分段头**

解析器若强依赖分段头 → **单只调用静默返回 0 行且不报错**（本次踩到，
表现为"sz 组分片丢失"）。修法：解析函数加 `fallback_code` 参数。

### 3.4 附带：`etf holdings --date` 不生效

传 `--date 2023-06-01` 与 `--date 2026-09-10` 返回**逐字节相同**的持仓
→ 判定为快照型，历史不可回补。判据可复用：**两个相差数年的日期返回完全相同数据**。

---

## 四、口径与交叉校验（实测）

### 4.1 净值 `closePrice` vs 日线 `close` —— **逐行完全相等**

两个**独立源**（westock 腾讯自选股 vs fsdb 本地引擎）的收盘价对比：

| 指标 | 结果 |
|---|---|
| 可配对 | 12,508 行 / 1,565 只（2026-09-01 ~ 09-10） |
| 绝对差 中位 / 最大 | **0.000000 / 0.000000** |
| 完全相等行数占比 | **100.0%** |

→ `premium_pct` 的分子分母（close 与 nav）可信。

### 4.2 折溢价率分布（全样本 150 万行）

| 分位 | P1 | P25 | 中位 | P75 | P99 |
|---|---|---|---|---|---|
| `premium_pct` | −2.52% | −0.10% | 0.00% | +0.07% | +4.10% |

极值对应分红除权日与流动性冲击日，量级合理。

### 4.3 ⚠️ "净值早于日线" 的 77 只 —— 绝大多数**不是净值脏**

初看像净值脏数据，拆开后是三类：

| 类型 | 只数 | 判读 |
|---|---|---|
| 日线被 fsdb **截断在 2024-01-02** | **23** | **净值才是完整的**（如 510880 红利ETF 净值 2007-01-18 起，日线 2024-01-02 起）→ 净值可用于补日线缺口 |
| 小 gap（42~49 天） | 53 | 正常：**基金成立日早于上市日**，净值从成立日就有 |
| 大 gap（3387 天） | **1**（159925） | **真脏数据**：代码复用导致 2004-01-02~2013-04-10 段是别的基金的净值 |

→ 结论：**净值的实际质量比预期更好**，且能反向标识哪些标的的日线缺口更大。
⚠️ 使用建议：以 `etf_daily` 的起始日为闸门过滤，或对 159925 单独排除。

### 4.4 复权乘数

ETF 复权表 `mult`/`cum` 由源直接给出，无需本地重建
（不同于股票：股票复权表缺 `peigu`，仍需 `compute_adj_factors`）。

---

## 五、交付物

| 类型 | 路径 |
|---|---|
| 模块 | `quantlab/data/etf_minute.py`、`etf_nav.py`、`etf_adj.py`、`etf_holding.py` |
| 探测 | `scripts/probe_etf_sources.py`、`scripts/diag_etf_status.py` |
| 回补入口 | `scripts/backfill_etf_dataset.py` |
| 流水线 | `quantlab/data/update.py`（3.10/3.11/3.12 三域） |
| 提交 | `9fcae72` |
| 教训沉淀 | 项目级 skill `quantlab-datasource-onboarding`（safe-delete 约束 + westock 取数陷阱） |

---

## 六、回补结果（最终）

| 数据 | 行数 | 标的 | 日期范围 | 耗时 |
|---|---|---|---|---|
| **分钟** | **122,245,103** | 2,049 | 2025-01-02 09:31 → 2026-09-10 15:00（411 个交易日） | 拉取 ~70min |
| **净值** | **1,501,949** | 1,566 | 2004-01-02 → 2026-09-11 | 47.7 min |
| **复权** | **1,083** | 289 | 2004 ~ 2026（年度分红） | 3.4 min |
| **持仓快照** | **29,138** | 1,469 | 2026-09-12（快照，前向积累） | 11 min |

（日线为既有数据：2,303,259 行 / 2,053 只 / 2004-03-22 起）

### 6.1 分钟存储：为什么最终选了"按 code 平铺"

最初设计是 DuckDB `COPY ... PARTITION_BY (_year,_day)` 按日分区，**实测失败**：

| 尝试 | 结果 | 问题 |
|---|---|---|
| pyarrow `to_table()` + `write_dataset` | 单文件/分区 ✅ | 但 `to_table()` 把 1.23 亿行一次性物化 → 进程 **10.57 GB**（机器可用 13.8 GB） |
| DuckDB `COPY ... PARTITION_BY` | 流式、内存可控 ✅ | 但 `preserve_insertion_order=false` 下**每分区产出 50+ 小文件** → 411 分区产生 **47,000+ 个 parquet**（1.3 GB） |
| 事后逐分区合并 | — | 每分区 114 个文件，合并 740/47078 用了 2.5 分钟 → **预计 1 小时**，放弃 |

关键危害不只是文件数：**`update()` 覆盖其中单个文件时，其余同日文件仍带着旧数据**
→ 重复 + 取数不确定。

**最终方案：按 code 平铺**（`part-{code}.parquet`）
- 拉取阶段的临时文件**本身就是** `{code}.parquet` → 用 `os.replace` 直接转正，
  **零重分区成本**（2049 个文件秒级完成）
- 文件数 2,049（而非 47,000）
- 单只读取只碰 1 个文件；`update()` 逐只「读旧 + 去当日 + 合并 + 写回」，单只约 6 万行，内存可控
- 代价：按 trade_date 过滤需扫全表（实测 1.23 亿行约 11 秒，可接受）

> 旧的分区版本改名为 `_kline_1min_etf_byday/` 保留（未删除，本机 safe-delete 不允许批量删除）。

### 6.2 结构约定（最终）

```
data/lake/clean/kline_1min_etf/part-{code}.parquet            按 code 平铺
data/lake/clean/etf_nav/year=YYYY/part-full.parquet           历史
data/lake/clean/etf_nav/year=YYYY/part-dYYYYMMDD.parquet      增量
data/lake/clean/etf_adj/part-full.parquet
data/lake/clean/etf_holding/snap=YYYY-MM-DD/part-full.parquet
```
`load()` / `coverage()` 均带 `(code, datetime)` 去重兜底。
