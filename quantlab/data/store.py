"""存储引擎：DuckDB 主库 + Parquet 因子湖
=====================================
设计决策：
- clean 层数据存 DuckDB 原生表：支持增量 UPSERT / 事务，600 万行 K 线查询毫秒级
- factor 层存 Parquet：每因子独立目录，只追加，供 L2+ 消费与外部工具读取
- 元数据：datasets(域注册) / watermarks(更新水位) / data_dict(数据字典) / quality_report
- 所有表 date 字段统一 DATE 类型，code 统一 6 位字符串
"""
from __future__ import annotations

import threading
from typing import Optional

import duckdb
import pandas as pd

from .. import config
from ..config import get_logger

log = get_logger(__name__)


class Store:
    """DuckDB 存储引擎（线程安全单例）

    并发策略：DuckDB 单写者模型 ——
    - 写入场景（更新任务）：Store() 锁冲突立即报错，任务必须串行
    - 查询场景：Store(readonly=True) 只读连接；
      写锁被占时由模块级 query() 自动降级 Parquet 镜像（无锁）
    """

    _instance: Optional["Store"] = None
    _lock = threading.Lock()

    def __new__(cls, readonly: bool = False, wait_lock: bool = True):
        with cls._lock:
            # 已缓存只读实例，但本次明确要求写模式 → 重连为写实例
            if (cls._instance is not None and not readonly
                    and getattr(cls._instance, "readonly", False)):
                cls._instance.close()      # close 内部清空 _instance
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._init_db(readonly, wait_lock)
                cls._instance = inst
            return cls._instance

    # ── 基础 ───────────────────────────────────────────────────────
    def _init_db(self, readonly: bool = False, wait_lock: bool = True):
        """readonly=True（查询场景）：写锁被占时等待重试
        （wait_lock=True 最多5分钟；False 单次快速失败供降级判断）
        readonly=False（写入场景）：锁冲突立即报错（更新任务必须串行）"""
        import time as _t
        if readonly:
            attempts = 20 if wait_lock else 1
            for i in range(attempts):
                try:
                    self.con = duckdb.connect(str(config.DUCKDB_PATH),
                                              read_only=True)
                    self.readonly = True
                    break
                except duckdb.IOException:
                    if i == attempts - 1:
                        raise
                    if i == 0:
                        log.info("DuckDB 写锁被占用（更新任务运行中），"
                                 "查询将等待锁释放…")
                    _t.sleep(15)
        else:
            self.con = duckdb.connect(str(config.DUCKDB_PATH))
            self.readonly = False
        self._init_meta_schema()

    def _writable(self):
        if getattr(self, "readonly", False):
            raise PermissionError(
                "当前为只读连接（另一进程正在写入）。"
                "请等待数据更新任务完成，或在新会话中执行写操作。")

    def _init_meta_schema(self):
        if getattr(self, "readonly", False):
            return          # 只读连接跳过 schema 初始化
        self.con.execute("CREATE SEQUENCE IF NOT EXISTS run_id_seq START 1")
        self.con.execute("""
        CREATE TABLE IF NOT EXISTS datasets(
            domain VARCHAR PRIMARY KEY,      -- 数据域名 e.g. kline_daily
            layer VARCHAR,                   -- clean / factor
            description VARCHAR,
            source VARCHAR,                  -- 数据源
            frequency VARCHAR,               -- daily / weekly / monthly / minute / quarterly
            schema_hint VARCHAR,
            created_at TIMESTAMP DEFAULT now()
        )""")
        self.con.execute("""
        CREATE TABLE IF NOT EXISTS watermarks(
            domain VARCHAR,
            partition VARCHAR DEFAULT '',    -- 域内分区键(可选) e.g. 股票代码段
            watermark DATE,                  -- 已更新到的数据日期
            updated_at TIMESTAMP DEFAULT now(),
            PRIMARY KEY(domain, partition)
        )""")
        self.con.execute("""
        CREATE TABLE IF NOT EXISTS data_dict(
            domain VARCHAR,
            column_name VARCHAR,
            meaning VARCHAR,
            PRIMARY KEY(domain, column_name)
        )""")
        self.con.execute("""
        CREATE TABLE IF NOT EXISTS quality_report(
            checked_at TIMESTAMP DEFAULT now(),
            domain VARCHAR,
            check_type VARCHAR,              -- completeness/freshness/validity/consistency
            status VARCHAR,                  -- pass/warn/fail
            detail VARCHAR
        )""")
        self.con.execute("""
        CREATE TABLE IF NOT EXISTS run_log(
            id INTEGER DEFAULT nextval('run_id_seq'),
            started_at TIMESTAMP DEFAULT now(),
            domain VARCHAR,
            action VARCHAR,                  -- init/update/backfill
            rows_written BIGINT DEFAULT 0,
            status VARCHAR DEFAULT 'running',-- running/ok/fail
            message VARCHAR
        )""")

    def q(self, sql: str, params=None) -> pd.DataFrame:
        """查询返回 DataFrame（agent 研究主入口）"""
        cur = self.con.execute(sql, params or [])
        if cur.description is None:
            return pd.DataFrame()
        return cur.df()

    def execute(self, sql: str, params=None):
        return self.con.execute(sql, params or [])

    # ── 元数据 ─────────────────────────────────────────────────────
    def register_dataset(self, domain: str, layer: str, source: str,
                         frequency: str, description: str = "",
                         schema_hint: str = ""):
        self._writable()
        self.con.execute(
            "INSERT OR REPLACE INTO datasets VALUES (?,?,?,?,?,?,now())",
            [domain, layer, description, source, frequency, schema_hint])

    def list_datasets(self) -> pd.DataFrame:
        return self.q("SELECT * FROM datasets ORDER BY domain")

    def set_watermark(self, domain: str, watermark, partition: str = ""):
        self._writable()
        self.con.execute(
            "INSERT OR REPLACE INTO watermarks VALUES (?,?,?,now())",
            [domain, partition, pd.to_datetime(watermark).date()])

    def get_watermark(self, domain: str, partition: str = "") -> Optional[pd.Timestamp]:
        df = self.q(
            "SELECT watermark FROM watermarks WHERE domain=? AND partition=?",
            [domain, partition])
        if df.empty:
            return None
        return pd.Timestamp(df.iloc[0, 0])

    def watermarks(self) -> pd.DataFrame:
        return self.q("""
            SELECT w.domain, w.partition, w.watermark, w.updated_at,
                   d.frequency, d.source
            FROM watermarks w LEFT JOIN datasets d USING(domain)
            ORDER BY w.domain""")

    # ── 数据写入（clean 层） ────────────────────────────────────────
    def ensure_table(self, table: str, ddl: str):
        if not self.readonly:
            self.con.execute(f"CREATE TABLE IF NOT EXISTS {table} ({ddl})")

    def upsert(self, df: pd.DataFrame, table: str, keys: list[str]):
        """按键 UPSERT：先 DELETE 命中键，再 INSERT（duckdb 事务保证）"""
        if df is None or df.empty:
            return 0
        assert all(k in df.columns for k in keys), f"缺少键列: {keys}"
        self._writable()

        self.con.execute("BEGIN TRANSACTION")
        try:
            # 注册临时表并删除命中行
            self.con.register("_upsert_df", df)
            key_cols = ", ".join(keys)
            self.con.execute(
                f"DELETE FROM {table} WHERE ({key_cols}) IN "
                f"(SELECT {key_cols} FROM _upsert_df)")
            # 必须按列名插入：SELECT * 依赖 df 列序与建表 DDL 完全一致，
            # 一旦不一致会静默错位写脏数据（财务快照曾因此报错）。
            cols = ", ".join(df.columns)
            self.con.execute(
                f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _upsert_df")
            self.con.unregister("_upsert_df")
            self.con.execute("COMMIT")
            return len(df)
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def replace_table(self, table: str, df: pd.DataFrame, ddl: str):
        """整表替换（小表用：股票主表、日历等）"""
        self._writable()
        self.con.execute(f"DROP TABLE IF EXISTS {table}")
        self.con.execute(f"CREATE TABLE {table} ({ddl})")
        if df is not None and not df.empty:
            self.con.register("_repl_df", df)
            cols = ", ".join(df.columns)
            self.con.execute(
                f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _repl_df")
            self.con.unregister("_repl_df")

    # ── 因子层（Parquet） ──────────────────────────────────────────
    def factor_dir(self, factor_name: str):
        d = config.FACTOR_DIR / factor_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def append_factor(self, factor_name: str, df: pd.DataFrame):
        """因子截面追加写 Parquet（按年份分区文件）"""
        if df is None or df.empty:
            return 0
        self._writable()
        d = self.factor_dir(factor_name)
        dates = pd.to_datetime(df["date"])
        written = 0
        for year in sorted(dates.dt.year.unique()):
            part = df[dates.dt.year == year]
            f = d / f"part-{year}.parquet"
            old = pd.read_parquet(f) if f.exists() else pd.DataFrame()
            if not old.empty:
                merged = pd.concat([old, part], ignore_index=True)
                merged = (merged.drop_duplicates(subset=["date", "code"],
                                                keep="last")
                          .sort_values(["date", "code"]))
            else:
                merged = part
            merged.to_parquet(f, index=False)
            written += len(part)
        return written

    def read_factor(self, factor_name: str,
                    start=None, end=None) -> pd.DataFrame:
        """读取因子截面（date/code/value 宽格式）"""
        d = config.FACTOR_DIR / factor_name
        if not d.exists():
            return pd.DataFrame()
        df = pd.read_parquet(d)
        if "value" not in df.columns and "score" in df.columns:
            # 模型分数因子（lgbm_*/gru_seq_*）落库为 score 列，统一暴露为 value
            df = df.rename(columns={"score": "value"})
        if start is not None:
            df = df[df["date"] >= pd.to_datetime(start)]
        if end is not None:
            df = df[df["date"] <= pd.to_datetime(end)]
        return df

    # ── 运行日志 ───────────────────────────────────────────────────
    def log_run(self, domain: str, action: str) -> int:
        r = self.q("SELECT nextval('run_id_seq') AS id").iloc[0, 0]
        self.con.execute(
            "INSERT INTO run_log(id, domain, action) VALUES (?,?,?)",
            [int(r), domain, action])
        return int(r)

    def finish_run(self, run_id: int, status: str, rows: int, message: str = ""):
        self.con.execute(
            "UPDATE run_log SET status=?, rows_written=?, message=? WHERE id=?",
            [status, rows, message, run_id])

    # ── 质量报告 ───────────────────────────────────────────────────
    def add_quality(self, domain: str, check_type: str, status: str, detail: str):
        if self.readonly:
            return
        self.con.execute(
            "INSERT INTO quality_report(domain, check_type, status, detail) VALUES (?,?,?,?)",
            [domain, check_type, status, detail])

    # ── Parquet 镜像（无锁查询路径） ─────────────────────────────
    MIRROR_TABLES = ["kline_daily", "daily_snapshot", "instruments",
                     "index_kline", "trade_calendar", "finance_snapshot",
                     "dividend_events", "dragon_tiger", "hot_topic",
                     "northbound_daily", "margin_total", "lockup",
                     "block_trade", "holder_num", "fund_flow_daily",
                     "index_members"]

    def export_mirror(self):
        """导出核心表 Parquet 镜像（更新任务完成后调用，供无锁查询）"""
        self._writable()
        import os
        d = config.CLEAN_DIR / "mirror"
        d.mkdir(parents=True, exist_ok=True)
        exported = []
        for t in self.MIRROR_TABLES:
            try:
                exist = self.q(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_name=?", [t]).iloc[0, 0]
                if not exist:
                    continue
                f = d / f"{t}.parquet"
                # 原子写：先写临时文件再替换
                tmp = d / f".{t}.parquet.tmp"
                self.con.execute(
                    f"COPY (SELECT * FROM {t}) TO '{tmp}' "
                    f"(FORMAT PARQUET)")
                os.replace(tmp, f)
                exported.append(t)
            except Exception as e:
                log.debug(f"镜像导出 {t} 失败: {e}")
        if exported:
            log.info(f"Parquet 镜像已导出: {len(exported)} 表")
        return exported

    @classmethod
    def mirror_query(cls, sql: str, params=None) -> pd.DataFrame:
        """无锁查询：内存库 + Parquet 镜像视图（更新任务持锁时的兜底）"""
        d = config.CLEAN_DIR / "mirror"
        con = duckdb.connect()
        n_views = 0
        for t in cls.MIRROR_TABLES:
            f = d / f"{t}.parquet"
            if f.exists():
                con.execute(
                    f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{f}')")
                n_views += 1
        if n_views == 0:
            con.close()
            raise RuntimeError(
                "DuckDB 写锁被占用且 Parquet 镜像尚未导出。"
                "请等待数据更新任务完成（完成后自动导出镜像）。")
        try:
            cur = con.execute(sql, params or [])
            return cur.df() if cur.description is not None else pd.DataFrame()
        finally:
            con.close()

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass
        Store._instance = None


# ── 模块级统一查询入口（三层降级） ─────────────────────────────────
def query(sql: str, params=None) -> pd.DataFrame:
    """研究/CLI 查询统一入口，永不因写锁被占而失败：
    1. 本进程已持有连接 → 直接查（读写实例均可查）
    2. 只读直连 DuckDB（写锁空闲时）
    3. Parquet 镜像（写锁被占时的无锁兜底，数据截至最近一次导出）
    """
    inst = Store._instance
    if inst is not None:
        return inst.q(sql, params)
    try:
        inst = Store(readonly=True, wait_lock=False)
        return inst.q(sql, params)
    except duckdb.IOException:
        return Store.mirror_query(sql, params)
