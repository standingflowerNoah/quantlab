# QuantLab 协作环境搭建与数据分发指南

> 面向合作者 · 2026-09-09 · 配合《quantlab_guide.html》使用
> 目标：在一台新 Windows 机器上把整套系统跑起来，并拿到研究必需的数据

## 0. 先想清楚：你要哪种程度的参与

| 程度 | 需要拿什么 | 体量 |
|---|---|---|
| 只看结果 | 每日 overview 报告公开链接（工作日自动更新） | 0 |
| 跑研究/复现因子 | 代码仓库 + 环境 + **数据（见下）** | 代码 ~10MB；数据 17~41GB 看路线 |
| 改核心代码 | 同上 + 阅读研究文档 research/*.md | 同上 |

## 1. 环境搭建（约 30 分钟）

```bash
# 1) Python 3.13 + 专用 venv（pypi 走清华镜像）
python -m venv C:\quantlab\envs\quantlab
C:\quantlab\envs\quantlab\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2) 克隆代码仓库（Gitee 私有仓库，地址另发）
git clone <REPO_URL> C:\quantlab
cd C:\quantlab

# 3) 冒烟测试（不依赖数据湖也应通过核心单测）
C:\quantlab\envs\quantlab\Scripts\python.exe tests/test_smoke.py
```

## 2. 数据获取：两条路线

### 体量清单（当前实测，随时间增长）

| 内容 | 路径 | 体量 | 能否重建 |
|---|---|---|---|
| 日K线/财务/特色数据 | `data/lake/clean`（不含分钟） | ~0.5G | ✅ `data init` 自建（需网络） |
| 分钟K线湖 | `data/lake/clean/kline_1min` | 6.5G | ⚠️ 需 fsdb 镜像间接重建 |
| 因子湖 | `data/lake/factor` | 9.5G | ✅ `factor compute-all` 重算（数小时） |
| 因子审查档案 | `data/lake/factor_audit` | ~1M | ✅ 随因子计算自动生成 |
| DuckDB 主库 | `data/quant.duckdb` | 483M | ✅ 由各步自动重建 |
| free-stockdb 镜像 | `tools/free-stockdb/` | ~24G | ❌ **必须分发**（LevelDB 私有格式） |

### 路线 A：全量拷贝（推荐，最省事）

适合：合作者与你同城市/可用移动硬盘/公司内网。

1. 打包（在你机器上执行）：
   ```bash
   # 需要分发的三块：镜像 24G + 分钟湖 6.5G + 因子湖 9.5G（可选）
   tar 或直接 robocopy 到移动硬盘
   ```
2. 合作者拿到后按原目录结构放到项目根下：
   - `tools/free-stockdb/`（含 `stockdb/` 与 `stockdb/data`、`data1` 两个 LevelDB 目录）
   - `data/lake/clean/kline_1min/`（可选，也可由镜像重建）
   - `data/lake/factor/`（可选，也可由 compute-all 重建）
3. 首次初始化日频数据 + 校验：
   ```bash
   $PY cli.py data init          # 幂等，断点续跑
   $PY cli.py data quality --sample 30
   ```

### 路线 B：远程重建（不搬硬盘）

1. 安装 free-stockdb 工具链并同步镜像（详见仓库内 `tools/free-stockdb` 说明，~24G 下载）：
   `数据更新.exe --sync`（注意：同步器完成后不自动退出，属正常，看门狗会回收）
2. `$PY cli.py data init` —— 日频全量首建（通达信/东财等免费源，需 1~2 小时）
3. `$PY cli.py data minute-init` → `minute-update` —— 分钟湖从镜像构建（2025-01 起）
4. `$PY cli.py factor compute-all` —— 因子全量重算（Alpha191 用 `scripts/alpha191_compute.py calc`，约数小时）

> 路线 B 首建后数字应与路线 A 一致（同源同算法）；差异大先跑 `data quality`。

## 3. 验证清单（数据就位后必跑）

```bash
$PY cli.py data status          # 各域水位=最近交易日
$PY cli.py universe             # ashare_ex ≈ 5000 只
$PY cli.py query "SELECT COUNT(*) FROM kline_daily"   # ≈ 585 万行
$PY cli.py factor summary reversal_5 --horizon 20    # IC ≈ +0.062
$PY cli.py factor backtest reversal_5 --horizon 20 --quantiles 5
$PY tests/test_smoke.py
```

全对上 → 环境可用。

## 4. 协作纪律（重要）

1. **DuckDB 单写者**：任何时刻只跑一个写任务（data update / factor compute / decision run）；查询类命令随时可跑。
2. **口径不可擅改**：`kline_daily.vol`=股、`amount`=元、价格不复权；改动需在研究文档中提案。
3. **研究结论入档**：每轮实验（含负面结论）写 `research/*.md` 并 commit；不要只在口头/聊天里传结论。
4. **生产模型变更走双闸门**：walk-forward 样本外通过 + 与生产基准 A/B 通过，缺一不上。
5. **git 纪律**：代码/研究文档变更随手 commit；`data/ tools/ reports/ logs/ portfolio_state/` 不入库。

## 5. 常见问题

| 现象 | 处理 |
|---|---|
| `File is already open in ... (PID xxx)` | 单写者在工作（可能是每日流水线），等它结束；读路径自动降级镜像不受影响 |
| 东财 push2 资金流 404 | 上游封禁，域自动 degraded，恢复后探测自愈 |
| 分钟数据 2022-2024 缺失 | 免费源无覆盖，属已知基线，不要当 bug 报 |
| pypi 超时 | 统一走清华镜像 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
