"""因子基类（L2 因子层）
=====================================
因子统一契约：
- 输出长格式 DataFrame：date(DATE) / code(VARCHAR) / value(DOUBLE)
- 存储：Parquet 按年分区（复用 Store.append_factor / read_factor）
- 输入：clean 层数据（kline_daily / daily_snapshot / finance_snapshot）

时序因子（动量/波动率等）用 DuckDB 窗口函数一次 SQL 全市场计算，
截面因子（市值/PE/PB）直接映射 daily_snapshot，避免 Python 逐行循环。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


def universe_sql(universe: list[str] | None):
    """生成 universe 过滤 SQL 片段与参数（code 为 6 位字符串）"""
    if not universe:
        return "", []
    placeholders = ", ".join(["?"] * len(universe))
    return f" AND code IN ({placeholders})", list(universe)


class Factor(ABC):
    """因子基类：子类实现 compute()，返回 date/code/value 长格式"""

    name: str = ""              # 唯一标识（小写下划线）
    description: str = ""       # 一句话说明
    category: str = "generic"   # price / volume / size / value / tech
    freq: str = "daily"         # daily / weekly

    @abstractmethod
    def compute(self, store, start=None, end=None,
                universe: list[str] | None = None) -> pd.DataFrame:
        """计算因子截面/时序序列

        返回 DataFrame，列必须为：date, code, value
        - date:  datetime64 或可被 pd.to_datetime 解析
        - code:  6 位字符串
        - value: float（NaN 表示当期无值，写库前会自动丢弃）
        """

    def __repr__(self):
        return f"<Factor {self.name}: {self.description}>"


class SqlFactor(Factor):
    """SQL 驱动因子：子类提供 _sql() 返回 SELECT date, code, value 的 SQL。

    约定：
    - SQL 内可用 {start}/{end} 占位（自动替换为 'YYYY-MM-DD' 字面量，已防注入）
    - 可选 {universe_sql}/{universe_params} 由基类注入 universe 过滤
    - value 允许 NaN，落库前统一 dropna
    """

    # 子类覆盖
    _base_sql: str = ""

    def _sql(self, start=None, end=None, universe=None) -> tuple[str, list]:
        raise NotImplementedError

    def compute(self, store, start=None, end=None,
                universe: list[str] | None = None) -> pd.DataFrame:
        sql, params = self._sql(start, end, universe)
        df = store.q(sql, params)
        if df.empty:
            return pd.DataFrame(columns=["date", "code", "value"])
        df = df.rename(columns={"value": "value"})
        df["date"] = pd.to_datetime(df["date"])
        df["code"] = df["code"].astype(str).str.zfill(6)
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna(subset=["value"])
        return df[["date", "code", "value"]].reset_index(drop=True)


# ── 通用：K 线前复权收盘价 CTE ──────────────────────────────────────
QFQ_CLOSE = """
WITH qfq AS (
    SELECT date, code, close * adj_factor AS close_adj
    FROM kline_daily
)
"""
