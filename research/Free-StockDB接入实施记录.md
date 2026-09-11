# Free-StockDB 冗余接入实施记录

> 2026-09-11 实施 | 前置裁决见 `research/Free-StockDB稳定性评估与接入清单.md`
> 用户批复：①逐日快照积累 ②申万值得 ③先做 ETF 日线 ④对账先 ⑤允许自动重启

---

## 0. 实施结果总览

| 阶段 | 内容 | 状态 | 核心产出 |
|---|---|---|---|
| **P0** | 稳定性加固 | ✅ 完成 | `fsdb_source.py` 四护栏 + 看门狗 |
| **P1** | 日线/快照/指数自动兜底 | ✅ 完成 | `fsdb_fallback.py` + `update.py` 内建降级 |
| **P2-1** | 复权因子对账 | ✅ 完成（**有条件通过**） | `scripts/reconcile_fsdb_adj.py` + 对账报告 |
| **P2-2** | 板块逐日快照 | ✅ 完成（**含一处结论修正**） | `quantlab/data/board.py` + 首份快照 |
| **P2-3** | ETF 日线入湖 | ✅ 完成 | `quantlab/data/etf.py` + 2,004 只 |

---

## 1. P0 稳定性加固（fsdb_source.py）

### 四条护栏

| 护栏 | 实现 | 实测 |
|---|---|---|
| ① 入参校验 | `_guard()` 拒绝无界 keys 枚举 / 无界日期范围 / 无 k1 的通配表扫描 | 4 类恶意查询 4/4 拦截，3 类正常查询 3/3 放行 |
| ② 并发闸门 | `threading.BoundedSemaphore(4)` 包住所有 HTTP 请求 | 调用方起 6 线程，实际打到引擎 ≤4 |
| ③ 超时+重试 | 单请求 15s，失败重试 2 次（退避 1s / 3s） | 失败 3 次才抛 `RuntimeError` |
| ④ 延迟看门狗 | `health_check()` 小样本探测 → 劣化则 `restart_service()` | 探测 p50 = 39.7ms（健康） |

被拦截的查询形态（实测）：
```
cmd=keys&t=*                        → 禁止无界键枚举
vals&t=*                            → 禁止无界表名查询
日k + k2=fwz:0,99999999             → 禁止无界日期范围
板块* （无 k1）                      → 通配表名必须带 k1 约束
```

`ensure_healthy()` 带 300s 节流，可安全放在批处理入口。

### 新增运维检查

- `disabled_dirs()` —— 检测各数据目录的 `disable` 停更标识。**实测已命中：`data` 目录被标记停更**（22GB 主库不再接收增量，日增量全走 `data1`）。
- `clean_part_files()` —— 同步前清理 `.part` 残留。**实测已命中 2 个**（`data1/003351.log.part`、`data1/MANIFEST-003350.part`）。
- `_watch_sync` 增加 `stale_s=600`：存在 `.part` 时放宽回收阈值，避免误杀仍在重试的连接（"完成"与"停滞"外部不可区分，宁可多等）。

---

## 2. P1 日线/快照/指数自动兜底

### 降级映射

| 生产域 | 主源 | 兜底源 | 校验方式 |
|---|---|---|---|
| `kline_daily` | 通达信 | **fsdb 日k** | 水位是否推进到目标交易日 |
| `daily_snapshot` | 东财/通达信 | **fsdb 日k**（含估值字段） | 同上 |
| `index_kline` | 通达信 | **新浪指数** | 同上 |

### 关键实现

- `run_checked()`：主源执行 → 校验水位 → 未推进则自动降级，并把降级动作写入 `quality_report(domain, 'source_fallback', 'warn', ...)`
- **兜底结果按日缓存**：`kline_daily` 与 `daily_snapshot` 共用同一次 fsdb 拉取，不重复跑 40s
- **日历前置刷新**：`domains` 未含 `calendar` 时也强制刷新一次——否则 `calendar_max == kline_max` 会让校验恒真、兜底永不触发（已修的漏洞）

### 端到端实测

```
[index_kline] 主源失败: 通达信服务器池全部不可用: None
[index_kline] 未完成（…）→ 自动降级兜底源
[fallback] index_kline 补 2026-09-11: 7 个指数，水位 2026-09-11
[fallback] trade_calendar 刷新: 4000 个交易日（至 2026-09-11）
更新完成: index_kline:degraded
→ quality_report: index_kline / source_fallback / warn
```

这次测试**顺带真实补齐了 09-11 的指数与日历**（此前湖内停在 09-10）。

### 口径校验（写入前置）

兜底写入前先验证 fsdb 与湖内日线是否同口径（40 只抽样，2026-09-10）：

| 字段 | 结果 |
|---|---|
| close 相对差 | 中位 **0.00 bp**、100% 完全一致 |
| vol 比值 | 中位 **1.0000**（范围 1.0000~1.0000） |
| amount 比值 | 中位 **1.0000** |

→ 兜底写入与现有 TDX 口径**完全一致**，可安全覆盖。

---

## 3. P2-1 复权因子对账（结论：有条件通过）

样本 60 只（含 600519 / 000001），随机种子固定。

### ① 事件日匹配

| 指标 | 值 |
|---|---|
| fsdb 事件 / lake 事件 | 613 / 642 |
| 双侧匹配 | **605** |
| 按股匹配率（中位） | **100.0%** |
| fsdb 独有 / lake 独有 | 1.3% / 5.8% |
| 有事件股占比 | fsdb 98.3% / lake 100% |

### ② 字段映射（精确）

| 映射 | 比值（中位，p10~p90） | 判定 |
|---|---|---|
| `fsdb.div` ÷ `lake.fenhong/10` | **10.0000**（10.0~10.0） | ✅ `div ≡ fenhong`，**同口径，无需换算** |
| `(fsdb.give+trans)` ÷ `lake.songzhuangu` | **1.0000** | ✅ 精确对应 |

### ③ 乘数方向

fsdb `mult` > 1（600519 为 1.013~1.024）→ 是**后向乘数 P_prev/B**；本地 `compute_adj_factors` 用的是 `B/P_prev = 1/mult`（前复权）。方向互为倒数，换算明确。

### ④ 自洽性

`∏mult` 与 `cum` 的偏差：中位 0.0004，**98.1% 的股票在 1% 以内**；仅 1 只（600649）偏差 78.9%。

### 裁决

| 用途 | 可否 | 说明 |
|---|---|---|
| 作为 `dividend_events` 的**分红/送转第二源** | ✅ 可以 | 字段精确对应、事件日匹配率 100%、覆盖 98.3% |
| 作为**事件日交叉校验** | ✅ 可以 | 可发现单源漏事件 |
| **独立复现复权因子** | ❌ 不可以 | **fsdb 无 `peigu`/`peigujia`（配股）字段**；本地 58,223 条事件中 988 条（**1.70%**）涉及配股。这正是 600649 自洽性崩掉的原因 |

→ **复权因子继续由本地 `compute_adj_factors` 计算**，fsdb 只作分红/送转字段的冗余源与对账参照。

---

## 4. P2-2 板块逐日快照（含一处结论修正）

### 落地内容

- `quantlab/data/board.py`：`snapshot()` / `board_catalog()` / `board_members()` / `sw_industry_map()` / `coverage()`
- 湖：`data/lake/clean/board/{board_meta,board_member}/snap=YYYY-MM-DD/part-dYYYYMMDD.parquet`
- 首份快照：**1,337 个板块 / 93,514 条成分记录**（概念 978 + 申万一级 28 / 二级 104 / 三级 227）

### ⚠️ PIT 保护（关键设计）

fsdb 只提供**当前**成分，没有任何历史版本。因此 `snapshot()` 内置 `clamp_to_today=True`：
传入更早的日期会被**夹回今天并告警**，从根上杜绝"用今天的成分伪造历史快照"。

```
板块快照请求日期 2026-09-10 ≠ 今天 2026-09-11：fsdb 只有当前成分，为避免伪造历史已夹到今天
```

ASOF 查询语义正确：只有 09-11 一份快照时，`asof=2026-09-10` 返回空（不向未来取数）。

### 🔴 结论修正：申万**不能**用作 industry_map 的补充

原评估把申万列为「不全（28/104/227 vs 官方约 31/134/346）」，用户据此批"值得"。
逐项对账后发现问题不是"不全"而是**版本过时**：

| 项 | fsdb | 本项目 `instruments` |
|---|---|---|
| 申万一级 | 28 个（2014 版） | **31 个（2021 版）** |
| 申万二级 | 104 | **128** |
| 申万三级 | 227 | **337** |
| 覆盖股票 | 5,011 | **5,559** |

**版本指纹**（只在一侧出现的行业名）：
- 仅 fsdb 有：`化工`、`商业贸易`、`采掘`、`休闲服务`、`电气设备`、`纺织服装` ← 2014 版
- 仅本地有：`基础化工`、`商贸零售`、`煤炭`、`社会服务`、`电力设备`、`纺织服饰`、`石油石化`、`环保`、`美容护理` ← 2021 版

**行业名一致率仅 76.7%**（5,008 只可比股）。

→ **裁决：申万部分不接入**；本地 `instruments.industry/l2/l3` 严格优于 fsdb。`sw_industry_map()` 保留但仅用于概念研究或分类版本差异诊断，函数 docstring 已写明警告。

### 真正拿到的增量：978 个概念板块

本地此前**完全没有**概念板块映射，这是本次接入的核心增量（用途：概念热点因子、题材轮动、事件驱动研究，均须按快照日期 ASOF 取用）。

---

## 5. P2-3 ETF 日线入湖

| 项 | 值 |
|---|---|
| 候选池 | fsdb `股票代码` 组 1 + 组 5 = **2,053 只** |
| 写入 | **2,004 只**（缺 49） |
| 其中 name 含 `ETF` | **1,564 只** |
| 耗时 | **16s**（全量单日） |
| 幂等 | 复跑新增 **0** 行 ✅ |
| 湖 | `data/lake/clean/etf_daily/year=YYYY/part-dYYYYMMDD.parquet` |

- `is_etf` 由**名称是否含 "ETF"** 判定（数据驱动，不靠代码前缀猜）
- 其余 440 只为 LOF/货币基金等，落库但 `load_etf_daily(etf_only=True)` 默认过滤
- 字段：date/code/name/open/high/low/close/pre_close/volume/amount/turnover/pct_chg/amplitude/total_share/total_mv/is_etf

---

## 6. 流水线集成

`update_all()` 新增两个域（接在 `kline_1min` 之后）：

```
3.6 etf_daily  ← fsdb 日线（16s）
3.7 board_map  ← 板块逐日快照（1s）
```

均已 `register_dataset` + `set_watermark`，`update_plan()` 可见。

---

## 7. 踩坑记录（本次新增）

1. **pandas 3.x 的 `read_parquet` 不支持 glob** —— 传 `path/*/part-*.parquet` 会走到 `open()` 报 `OSError [Errno 22]`。必须改用 in-memory DuckDB（`read_parquet('...', hive_partitioning=false)`），与项目既有分析惯例一致。
2. **hive 分区目录名不能与真实列名同名** —— 原用 `date=YYYY-MM-DD/` 与列 `date` 冲突，改为 `snap=YYYY-MM-DD/`。
3. **兜底校验的"恒真陷阱"** —— 若目标交易日取自水位本身，校验必然通过、兜底永不触发；必须让日历先刷新，用日历最大值作基准。
4. **写"历史"快照的前视风险** —— 无历史版本的数据源必须夹到当天写入，否则等于伪造历史。已内建 `clamp_to_today`。

---

## 8. 交付物

| 类型 | 路径 |
|---|---|
| 加固数据源 | `quantlab/data/sources/fsdb_source.py` |
| 兜底模块 | `quantlab/data/sources/fsdb_fallback.py` |
| 板块模块 | `quantlab/data/board.py` |
| ETF 模块 | `quantlab/data/etf.py` |
| 流水线 | `quantlab/data/update.py`（`run_checked` + 2 新域） |
| 对账脚本 | `scripts/reconcile_fsdb_adj.py` |
| 对账报告 | `reports/fsdb复权对账/summary.csv`、`detail_600519.csv` |

## 9. 遗留事项

- **`data/disable` 未删除**：22GB 主库仍停更，目前靠 `data1` 承接增量。删除该文件可恢复主库同步，但会触发一次全量校验——需单独决策，本次仅做检测告警。
- **概念板块历史无法回补**：只能从 2026-09-11 起前向积累；回测若需历史成分，须另找付费源或接受"仅用于当下截面"。
- **ETF 分钟未做**（用户裁决先日线）；如需接入，2,053 只 × 242 根/日，量级与现有 1 分钟湖同阶。
