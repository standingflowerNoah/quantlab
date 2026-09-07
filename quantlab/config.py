"""全局配置：路径、数据源参数、研究参数集中管理"""
from pathlib import Path

# ── 路径 ────────────────────────────────────────────────────────────
QUANT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = QUANT_ROOT / "data"
LAKE_DIR = DATA_DIR / "lake"          # Parquet 数据湖
RAW_DIR = LAKE_DIR / "raw"            # 原始层（排障/重放）
CLEAN_DIR = LAKE_DIR / "clean"        # 清洗层 parquet 导出
FACTOR_DIR = LAKE_DIR / "factor"      # 因子层
DUCKDB_PATH = DATA_DIR / "quant.duckdb"
RESEARCH_DIR = QUANT_ROOT / "research"
IDEAS_DIR = RESEARCH_DIR / "ideas"
REPORTS_DIR = QUANT_ROOT / "reports"
BACKTESTS_DIR = QUANT_ROOT / "backtests"
PORTFOLIO_STATE_DIR = QUANT_ROOT / "portfolio_state"
LOGS_DIR = QUANT_ROOT / "logs"

for _d in (DATA_DIR, LAKE_DIR, RAW_DIR, CLEAN_DIR, FACTOR_DIR,
           RESEARCH_DIR, IDEAS_DIR, REPORTS_DIR, BACKTESTS_DIR,
           PORTFOLIO_STATE_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ── 研究参数 ────────────────────────────────────────────────────────
HISTORY_START = "2022-01-01"          # K线回补起点（水位机制可向前扩）

# ── 数据源参数 ──────────────────────────────────────────────────────
# 东财防封：串行限流最小间隔（秒），批量任务可调大
EM_MIN_INTERVAL = 1.0
EM_TIMEOUT = 15

# 腾讯行情每批代码数（单次 URL 长度限制内）
TENCENT_BATCH = 60

# 通达信 TCP 重试（空结果自动重连故障转移）
TDX_RETRIES = 3

# ── free-stockdb（分钟数据源，本地引擎）─────────────────────────────
# 发行包位置（stockdb.exe / 数据更新.exe / pybao SDK / ./data leveldb）
FSDB_DIR = QUANT_ROOT / "tools" / "free-stockdb" / "win" / "stockdb"
FSDB_UPDATER = FSDB_DIR / "数据更新.exe"
FSDB_SERVICE = FSDB_DIR / "stockdb.exe"
FSDB_PYBAO = FSDB_DIR / "pybao"          # stock_sdk.py + stockdb.pyd
FSDB_HOST = "127.0.0.1"
FSDB_PORT = 7899
FSDB_SYNC_TIMEOUT = 3600 * 4             # 全量同步上限（秒），增量远小于此
# 分钟层时间范围与日频对齐（kline_daily 2022-01-04 起）
MINUTE_START = HISTORY_START
# 分钟 Parquet 湖（source of truth；DuckDB 仅建视图）
KLINE_1MIN_DIR = CLEAN_DIR / "kline_1min"

# ── Universe 内置股票池 ────────────────────────────────────────────
# 定义见 quantlab/data/universe.py
UNIVERSE_DEFAULT = "ashare_ex"        # 默认股票池：全A剔除ST/次新

# 指数代码表：代码 → (名称, 通达信市场号)  K线与基准用
INDEX_CODES = {
    "000001.SH": ("上证指数", 1),
    "399001.SZ": ("深证成指", 0),
    "399006.SZ": ("创业板指", 0),
    "000300.SH": ("沪深300", 1),
    "000905.SH": ("中证500", 1),
    "000852.SH": ("中证1000", 1),
    "000688.SH": ("科创50", 1),
    "880003.SH": ("通达信平均股价(近似全A等权)", 1),
}
BENCHMARK = "000300.SH"               # 默认业绩基准（全市场策略另配自算等权基准）

# ── 交易参数（模拟盘） ─────────────────────────────────────────────
INIT_CASH = 1_000_000                 # 模拟盘初始资金 100 万
COMMISSION = 2.5e-4                   # 佣金 万2.5（双向）
STAMP_TAX = 1e-3                      # 印花税 千1（仅卖出）
MIN_COMMISSION = 5.0                  # 最低佣金 5 元
SLIPPAGE_BASE = 10e-4                 # 基础滑点 10bp（按流动性分档调整）

# ── 日志 ────────────────────────────────────────────────────────────
import logging

def get_logger(name: str = "quantlab") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%m-%d %H:%M:%S")
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
        fh = logging.FileHandler(LOGS_DIR / "quantlab.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s"))
        logger.addHandler(fh)
    return logger
