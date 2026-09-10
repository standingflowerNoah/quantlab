"""因子质量评估：IC（信息系数）/ 覆盖率 / 分层收益
=====================================
IC = 每日截面「因子值」与「未来 horizon 日收益」的 Spearman 秩相关。
输出：IC 均值 / IC 标准差 / ICIR / IC>0 占比 / 样本覆盖。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import get_logger
from ..data.store import Store, query
from .registry import get_factor

log = get_logger(__name__)


def _fwd_return(horizon: int) -> pd.DataFrame:
    """前复权未来 horizon 日收益（date/code/fwd）

    用 DuckDB 窗口函数 LEAD 直接算（替代 pandas groupby shift，
    580 万行场景下快一个数量级）。
    """
    df = query(f"""
        SELECT date, code, c_lead / c - 1 AS fwd
        FROM (
            SELECT date, code, close * adj_factor AS c,
                   LEAD(close * adj_factor, {horizon}) OVER (
                       PARTITION BY code ORDER BY date) AS c_lead
            FROM kline_daily
        )
        WHERE c_lead IS NOT NULL AND c > 0
    """)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "code", "fwd"]].dropna()


def _load_factor(name: str, store: Store) -> pd.DataFrame:
    """优先读已落库因子，缺失则现算"""
    fv = store.read_factor(name)
    if fv.empty:
        f = get_factor(name)
        fv = f.compute(store)
    if fv.empty:
        return fv
    fv["date"] = pd.to_datetime(fv["date"])
    fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
    # 剔除无穷/异常值（如 ln(0)）
    fv = fv[np.isfinite(fv["value"])]
    return fv


def _factor_ic_sql(name: str, horizon: int, store: Store,
                   min_n: int) -> pd.DataFrame | None:
    """用 DuckDB 全量算 rank IC（窗口 rank + corr 聚合，比 pandas 逐日循环快一个量级）"""
    from pathlib import Path
    factor_dir = Path(store.factor_dir(name))
    files = sorted(factor_dir.glob("part-*.parquet"))
    if not files:
        return None
    paths = ", ".join("'" + str(f).replace("\\", "/") + "'" for f in files)
    # 值列兼容：标准因子为 value；模型分数因子（lgbm_*/gru_seq_*）为 score
    desc = store.q(
        f"DESCRIBE SELECT * FROM read_parquet(['{files[0].as_posix()}'])")
    vcol = "value" if "value" in set(desc["column_name"]) else "score"
    sql = f"""
    WITH fwd AS (
        SELECT date, code, c_lead / c - 1 AS fwd
        FROM (
            SELECT date, code, close * adj_factor AS c,
                   LEAD(close * adj_factor, {horizon}) OVER (
                       PARTITION BY code ORDER BY date) AS c_lead
            FROM kline_daily
        )
        WHERE c_lead IS NOT NULL AND c > 0
    ),
    joined AS (
        SELECT CAST(f.date AS DATE) AS date, f.code, f.{vcol} AS value, w.fwd
        FROM read_parquet([{paths}]) f
        JOIN fwd w ON CAST(f.date AS DATE) = w.date AND f.code = w.code
    ),
    ranked AS (
        SELECT date, value, fwd,
               rank() OVER (PARTITION BY date ORDER BY value) AS rv,
               rank() OVER (PARTITION BY date ORDER BY fwd) AS rf
        FROM joined
        WHERE value IS NOT NULL AND fwd IS NOT NULL
    )
    SELECT date, COUNT(*) AS n, corr(rv, rf) AS ic
    FROM ranked
    GROUP BY date
    HAVING COUNT(*) >= {min_n}
    ORDER BY date
    """
    try:
        df = store.q(sql)
        return df if not df.empty else None
    except Exception as e:
        log.debug(f"DuckDB IC 计算失败，回退 pandas: {e}")
        return None


def factor_ic(name: str, horizon: int = 5,
              start=None, end=None, min_n: int = 30) -> pd.DataFrame:
    """逐日截面 Rank IC 序列"""
    store = Store()
    df = _factor_ic_sql(name, horizon, store, min_n)
    if df is None:
        # 回退 pandas 路径
        fv = _load_factor(name, store)
        if fv.empty:
            log.warning(f"因子 {name} 无数据")
            return pd.DataFrame(columns=["date", "ic", "n"])
        fwd = _fwd_return(horizon)
        m = fv.merge(fwd, on=["date", "code"], how="inner")
        if m.empty:
            return pd.DataFrame(columns=["date", "ic", "n"])
        if start is not None:
            m = m[m["date"] >= pd.to_datetime(start)]
        if end is not None:
            m = m[m["date"] <= pd.to_datetime(end)]
        rows = []
        for d, g in m.groupby("date"):
            if len(g) < min_n:
                continue
            ic = g["value"].rank().corr(g["fwd"].rank())
            rows.append({"date": d, "ic": ic, "n": len(g)})
        df = pd.DataFrame(rows)
    else:
        df["date"] = pd.to_datetime(df["date"])
    if df.empty:
        return pd.DataFrame(columns=["date", "ic", "n"])
    if start is not None:
        df = df[df["date"] >= pd.to_datetime(start)]
    if end is not None:
        df = df[df["date"] <= pd.to_datetime(end)]
    return df.reset_index(drop=True)


def factor_summary(name: str, horizon: int = 5,
                   start=None, end=None) -> pd.DataFrame:
    """因子质量摘要：IC 统计 + 覆盖率"""
    ics = factor_ic(name, horizon, start, end)
    if ics.empty:
        return pd.DataFrame([{
            "factor": name, "horizon": horizon,
            "n_days": 0, "ic_mean": np.nan, "ic_std": np.nan,
            "icir": np.nan, "ic_win_rate": np.nan, "coverage": 0.0}])

    ic = ics["ic"]
    summary = {
        "factor": name,
        "horizon": horizon,
        "n_days": len(ics),
        "ic_mean": round(float(ic.mean()), 4),
        "ic_std": round(float(ic.std()), 4),
        "icir": round(float(ic.mean() / ic.std()) if ic.std() > 0 else np.nan, 4),
        "ic_win_rate": round(float((ic > 0).mean()), 4),
        "avg_n": int(ics["n"].mean()),
    }

    # 覆盖率：因子有值的股票数 / 股票池总数
    store = Store()
    fv = _load_factor(name, store)
    from ..data.instruments import all_codes
    total = len(all_codes(st_only=False))
    cov = fv["code"].nunique() / total if total else 0
    summary["coverage"] = round(float(cov), 4)

    return pd.DataFrame([summary])


def factor_icir_weights(names: list[str], horizon: int = 5,
                        store: Store | None = None) -> dict:
    """批量计算因子 ICIR（|mean/std|），用于 IC 加权合成"""
    if store is None:
        store = Store()
    fwd = _fwd_return(horizon)
    weights = {}
    for name in names:
        fv = _load_factor(name, store)
        if fv.empty:
            weights[name] = 0.0
            continue
        m = fv.merge(fwd, on=["date", "code"], how="inner")
        if m.empty:
            weights[name] = 0.0
            continue
        ics = []
        for _, g in m.groupby("date"):
            if len(g) >= 30:
                ics.append(g["value"].rank().corr(g["fwd"].rank()))
        ic = pd.Series(ics)
        weights[name] = (abs(float(ic.mean() / ic.std()))
                         if len(ic) and ic.std() > 0 else 0.0)
    return weights


def factor_ic_decay(name: str, horizons: list[int] | None = None) -> pd.DataFrame:
    """因子 IC 衰减：不同持有期的 IC 均值 / ICIR / 胜率

    用于判断因子最优持有期（IC 峰值对应的 horizon），指导调仓频率。
    """
    if horizons is None:
        horizons = [1, 3, 5, 10, 20, 60]
    rows = []
    for h in horizons:
        ics = factor_ic(name, horizon=h)
        if ics.empty:
            continue
        ic = ics["ic"]
        rows.append({
            "horizon": h,
            "ic_mean": round(float(ic.mean()), 4),
            "icir": round(float(ic.mean() / ic.std()), 3) if ic.std() > 0 else np.nan,
            "win_rate": round(float((ic > 0).mean()), 3),
        })
    return pd.DataFrame(rows)
