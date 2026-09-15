# -*- coding: utf-8 -*-
"""PandaAI 因子复现入库（日线 6 个 + 分钟 1 个 + alpha140 补算）。

主库被锁时走 mirror 数据源（data/lake/clean/mirror/kline_daily.parquet，
586 万行 2022-2026 含 adj_factor），因子直接写 parquet 湖，不碰 DuckDB 主库。

复现名单（PandaAI 两年+三年正向双达标、库内无同公式因子）:
  pa_a101_040          = WorldQuant 101#40: (-1*rank(stddev(high,10))) * corr(high,volume,10)
  pa_a101_044          = WorldQuant 101#44: -1 * corr(high, rank(volume), 5)
  pa_a101_088          = WorldQuant 101#88: min(rank(decay_linear((rank(open)+rank(low))-(rank(high)+rank(close)),8.07)),
                                                ts_rank(decay_linear(corr(ts_rank(close,8.45), ts_rank(adv60,20.70), 8.01),6.65),2.62))
  pa_5d_min_low_ratio  = ts_min(low,5)/close          (PandaAI cal_5d_min_low_ratio)
  pa_30d_close_avg_ratio = ts_mean(close,30)/close     (PandaAI cal_30d_close_avg_ratio)
  pa_30d_vol_dec_ratio = sum(max(-Δvol,0),30)/sum(vol,30)  (PandaAI cal_30d_vol_dec_ratio, 公式推断)
  alpha140             = 国君191#140, 公式库已有, compute_alpha191 补算
  pa_early_afternoon_ret = 13:00-14:00 时段收益 (PandaAI cal_early_afternoon_return, 分钟湖 2025+)

用法:
  C:/Users/53497/.workbuddy/binaries/python/envs/quantlab/Scripts/python.exe panda_pull/replicate_factors.py [--skip-minute]
"""
import sys
import time

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, ".")

from quantlab.config import FACTOR_DIR  # noqa: E402
from quantlab.factor.alpha191_ops import (  # noqa: E402
    ts_mean, ts_std, ts_corr, ts_rank, ts_min, ts_max, ts_sum, rank,
)
from quantlab.factor.alpha191 import WIDE_KEYS, _to_long, _sanity  # noqa: E402
from quantlab.factor.alpha191_formulas import ALPHAS  # noqa: E402

START = "2022-06-01"   # 宽表预热起点（与 alpha191 引擎一致）
LAKE_1MIN = "data/lake/clean/kline_1min"
MIRROR_DAILY = "data/lake/clean/mirror/kline_daily.parquet"


def append_factor_lake(factor_name: str, long_df: pd.DataFrame) -> int:
    """Store.append_factor 的无锁版：直接写 data/lake/factor/{name}/part-{year}.parquet"""
    d = FACTOR_DIR / factor_name
    d.mkdir(parents=True, exist_ok=True)
    long_df = long_df.dropna(subset=["value"])
    dates = pd.to_datetime(long_df["date"])
    written = 0
    for year in sorted(dates.dt.year.unique()):
        part = long_df[dates.dt.year == year].sort_values(["date", "code"])
        f = d / f"part-{year}.parquet"
        if f.exists():
            old = pd.read_parquet(f)
            merged = (pd.concat([old, part], ignore_index=True)
                      .drop_duplicates(subset=["date", "code"], keep="last")
                      .sort_values(["date", "code"]))
        else:
            merged = part
        merged.to_parquet(f, index=False)
        written += len(part)
    return written


def build_wide_mirror(start: str = START) -> dict[str, pd.DataFrame]:
    """mirror kline_daily → 宽表（逻辑与 alpha191.build_wide 一致）"""
    con = duckdb.connect()
    df = con.sql(f"""
        SELECT date, code, open, high, low, close, vol, amount, adj_factor
        FROM read_parquet('{MIRROR_DAILY}')
        WHERE close > 0 AND vol > 0
    """).fetchdf()
    con.close()
    df["date"] = pd.to_datetime(df["date"])
    if start:
        df = df[df["date"] >= pd.to_datetime(start)]
    df = df.dropna(subset=["adj_factor"])
    af = df.set_index(["date", "code"])["adj_factor"]
    wide = {}
    for col in ("open", "high", "low", "close"):
        w = df.set_index(["date", "code"])[col].unstack()
        wide[col] = (w * af.unstack()).sort_index()
    vw = (df["amount"] / df["vol"]).values
    tmp = df.assign(vwap=vw).set_index(["date", "code"])["vwap"].unstack()
    wide["vwap"] = (tmp * af.unstack()).sort_index()
    wide["volume"] = df.set_index(["date", "code"])["vol"].unstack().sort_index()
    wide["amount"] = df.set_index(["date", "code"])["amount"].unstack().sort_index()
    wide["ret"] = wide["close"].pct_change()
    return wide


def decay_linear(x: pd.DataFrame, n: float) -> pd.DataFrame:
    """线性衰减加权 MA：权重 1..n 归一（最新值权重最大），向量化 shift 实现"""
    k = int(n)
    w = np.arange(1, k + 1, dtype=float)
    w = w / w.sum()
    out = None
    for i, wi in enumerate(w):
        s = x.shift(k - 1 - i) * wi
        out = s if out is None else out + s
    return out


# ── 日线因子定义（输入宽表 W，输出 date×code 宽表）────────────────────────
def pa_a101_040(W):
    return (-1 * rank(ts_std(W["high"], 10))) * ts_corr(W["high"], W["volume"], 10)


def pa_a101_044(W):
    return -1 * ts_corr(W["high"], rank(W["volume"]), 5)


def pa_a101_088(W):
    # 101 原文窗口为小数，工程取整：8.07→8, 8.45→8, 20.70→21, 8.01→8, 6.65→7, 2.62→3
    adv60 = ts_mean(W["volume"], 60)
    t1 = rank(decay_linear((rank(W["open"]) + rank(W["low"]))
                           - (rank(W["high"]) + rank(W["close"])), 8))
    t2 = ts_rank(decay_linear(ts_corr(ts_rank(W["close"], 8),
                                      ts_rank(adv60, 21), 8), 7), 3)
    return t1.where(t2.isna() | (t1 <= t2), t2)


def pa_5d_min_low_ratio(W):
    return ts_min(W["low"], 5) / W["close"]


def pa_30d_close_avg_ratio(W):
    return ts_mean(W["close"], 30) / W["close"]


def pa_30d_vol_dec_ratio(W):
    dv = W["volume"].diff()
    dec = (-dv).clip(lower=0)
    return ts_sum(dec, 30) / ts_sum(W["volume"], 30)


DAILY = {
    "pa_a101_040": pa_a101_040,
    "pa_a101_044": pa_a101_044,
    "pa_a101_088": pa_a101_088,
    "pa_5d_min_low_ratio": pa_5d_min_low_ratio,
    "pa_30d_close_avg_ratio": pa_30d_close_avg_ratio,
    "pa_30d_vol_dec_ratio": pa_30d_vol_dec_ratio,
}


def run_daily() -> pd.DataFrame:
    print("[wide] building from mirror kline_daily...")
    t0 = time.time()
    wide = build_wide_mirror(START)
    print(f"[wide] done in {time.time() - t0:.0f}s, dates {wide['close'].index[0]} ~ {wide['close'].index[-1]}")
    rows = []
    todo = dict(DAILY)
    todo["alpha140"] = ALPHAS["alpha140"][0]   # 国君191#140，公式库现成
    for name, fn in todo.items():
        t0 = time.time()
        try:
            res = fn(wide)
            ok, note = _sanity(res)
            if not ok:
                rows.append({"name": name, "status": "invalid", "rows": 0, "note": note})
                print(f"[daily] {name} invalid: {note}")
                continue
            long = _to_long(res)
            n = append_factor_lake(name, long)
            rows.append({"name": name, "status": "ok", "rows": n, "note": note})
            print(f"[daily] {name}: {n} rows, {note}, {time.time() - t0:.0f}s")
        except Exception as e:  # noqa: BLE001
            rows.append({"name": name, "status": "FAIL", "rows": 0, "note": str(e)[:100]})
            print(f"[daily] {name} FAIL: {e}")
    return pd.DataFrame(rows)


def run_minute() -> pd.DataFrame:
    """13:00-14:00 时段收益 = 13:00-14:00 末根 close / 上午末根(11:30附近) close - 1"""
    con = duckdb.connect()
    frames = []
    for year in (2025, 2026):
        t0 = time.time()
        sql = f"""
        SELECT code, CAST(datetime AS DATE) AS d,
               arg_max(close, datetime) FILTER (
                   WHERE CAST(datetime AS TIME) >= TIME '11:00'
                     AND CAST(datetime AS TIME) <= TIME '11:31') AS c_am,
               arg_max(close, datetime) FILTER (
                   WHERE CAST(datetime AS TIME) >= TIME '13:00'
                     AND CAST(datetime AS TIME) <= TIME '14:00') AS c_pm
        FROM read_parquet('{LAKE_1MIN}/year={year}/part-*.parquet', hive_partitioning=false)
        WHERE close > 0
        GROUP BY code, d
        """
        df = con.sql(sql).fetchdf()
        df = df.dropna(subset=["c_am", "c_pm"])
        df["value"] = df["c_pm"] / df["c_am"] - 1.0
        frames.append(df[["d", "code", "value"]].rename(columns={"d": "date"}))
        print(f"[minute] {year}: {len(df)} rows ({time.time() - t0:.0f}s)")
    long = pd.concat(frames, ignore_index=True)
    long["date"] = pd.to_datetime(long["date"])
    long["value"] = pd.to_numeric(long["value"], errors="coerce")
    long = long.dropna(subset=["value"])
    n = append_factor_lake("pa_early_afternoon_ret", long)
    print(f"[minute] pa_early_afternoon_ret: {n} rows saved")
    return pd.DataFrame([{"name": "pa_early_afternoon_ret", "status": "ok",
                          "rows": n, "note": "13-14h return, 2025+"}])


def main():
    skip_minute = "--skip-minute" in sys.argv
    results = [run_daily()]
    if not skip_minute:
        results.append(run_minute())
    out = pd.concat(results, ignore_index=True)
    out.to_csv("reports/_tmp/pa_replication_summary.csv", index=False)
    print(out.to_string())


if __name__ == "__main__":
    main()
