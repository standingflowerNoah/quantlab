#!/usr/bin/env python3
"""Phase 1 快筛：方正系列日频近似因子（研报因子挖掘计划，2026-09-10）
=====================================
对 quantlab/factor/library/behavior.py 的 6 个新因子：
1. 全历史计算（内存中，不落湖）
2. Rank IC（fwd 5/20 日，与库内 ic_audit 口径一致：前复权 LEAD 收益）
3. 分年 IC 表
4. 与现有因子湖的去重检查（|ρ|≤0.9 门限，抽样日池化 Spearman）
5. 汇总表落 reports/phase1_screen.csv

用法：python scripts/phase1_screen.py
"""
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import duckdb
import numpy as np
import pandas as pd

NEW_FACTORS = ["team_coin_20", "climb_peak_20", "volvol_20",
               "amp_nonjump_20", "jump_share_20", "boat_follow_20"]

# 去重抽样：每月首个交易日，近 24 个月
SAMPLE_SQL = """
WITH dates AS (
    SELECT DISTINCT date FROM kline_daily ORDER BY date
),
picked AS (
    SELECT date FROM (
        SELECT date, ROW_NUMBER() OVER (PARTITION BY date_trunc('month', date)
                                        ORDER BY date) AS rn
        FROM dates
    ) WHERE rn = 1
)
SELECT date FROM picked ORDER BY date
"""


def fwd_returns(con: duckdb.DuckDBPyConnection, horizon: int) -> pd.DataFrame:
    return con.execute(f"""
        SELECT date, code, c_lead / c - 1 AS fwd
        FROM (
            SELECT date, code, close * adj_factor AS c,
                   LEAD(close * adj_factor, {horizon}) OVER (
                       PARTITION BY code ORDER BY date) AS c_lead
            FROM kline_daily
        )
        WHERE c_lead IS NOT NULL AND c > 0
    """).df()


def daily_ic(df: pd.DataFrame, fwd: pd.DataFrame) -> pd.DataFrame:
    m = df.merge(fwd, on=["date", "code"], how="inner")
    m = m[np.isfinite(m["value"]) & np.isfinite(m["fwd"])]
    rows = []
    for d, g in m.groupby("date"):
        if len(g) < 30:
            continue
        rows.append({"date": d,
                     "ic": g["value"].rank().corr(g["fwd"].rank()),
                     "n": len(g)})
    return pd.DataFrame(rows)


def yearly(ic: pd.DataFrame) -> dict[str, float]:
    if ic.empty:
        return {}
    g = ic.copy()
    g["year"] = pd.to_datetime(g["date"]).dt.year
    out = {}
    for y, gg in g.groupby("year"):
        out[str(y)] = round(float(gg["ic"].mean()), 4)
    return out


def main():
    from quantlab.data.store import Store
    from quantlab.factor.registry import get_factor

    store = Store()
    con = duckdb.connect()  # in-memory 只读分析（写锁惯例）
    con.register("kline_daily", store.q("SELECT * FROM kline_daily"))
    fwd5 = fwd_returns(con, 5)
    fwd20 = fwd_returns(con, 20)

    # 去重基座：现有因子湖抽样宽表（只读近 3 年分区，控内存）
    sample_dates = con.execute(SAMPLE_SQL).df()
    con.register("sample_dates_sd", sample_dates)
    dmin = sample_dates["date"].min()
    print(f"去重抽样 {len(sample_dates)} 个月初日（{dmin.date()} 起）", flush=True)
    lake_dirs = sorted(d for d in Path("data/lake/factor").iterdir() if d.is_dir())
    lake_names = [d.name for d in lake_dirs
                  if d.name not in NEW_FACTORS and list(d.glob("part-*.parquet"))]
    unions = []
    for name in lake_names:
        globs = [str(p).replace("\\", "/")
                 for p in sorted(Path(f"data/lake/factor/{name}").glob("part-*.parquet"))
                 if any(y in p.name for y in ("2024", "2025", "2026"))]
        if not globs:
            continue
        paths = ", ".join(f"'{g}'" for g in globs)
        unions.append(
            f"SELECT '{name}' AS factor, CAST(date AS DATE) AS date, code, "
            f"CAST(value AS REAL) AS v FROM read_parquet([{paths}])"
        )
    print(f"装载现有湖 {len(unions)}/{len(lake_names)} 个因子（2024+ 分区）...", flush=True)
    long_df = con.execute(
        "SELECT * FROM (" + " UNION ALL ".join(unions) + ") "
        "WHERE date IN (SELECT date FROM sample_dates_sd) AND v IS NOT NULL"
    ).df()
    wide_lake = long_df.pivot_table(index=["date", "code"], columns="factor",
                                    values="v", aggfunc="first")
    del long_df
    # 归一化日期键（duckdb 返回 us，pandas ns，不归一会静默 join 失败）
    wide_lake = wide_lake.reset_index()
    wide_lake["date"] = pd.to_datetime(wide_lake["date"]).dt.normalize()
    wide_lake = wide_lake.set_index(["date", "code"])
    print(f"现有湖宽表 {wide_lake.shape[0]} 行 × {wide_lake.shape[1]} 列", flush=True)

    results = []
    for name in NEW_FACTORS:
        fac = get_factor(name)
        print(f"计算 {name} ...", flush=True)
        df = fac.compute(store)
        if df.empty:
            results.append({"factor": name, "note": "EMPTY"})
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df[np.isfinite(df["value"])]
        df["date"] = df["date"].dt.date

        ic5 = daily_ic(df, fwd5.assign(date=fwd5["date"].dt.date))
        ic20 = daily_ic(df, fwd20.assign(date=fwd20["date"].dt.date))

        def stat(ic):
            if ic.empty:
                return (float("nan"),) * 4
            s = ic["ic"]
            return (round(s.mean(), 4), round(s.mean() / s.std(), 3),
                    round((s > 0).mean(), 3), len(ic))

        m5, ir5, w5, n5 = stat(ic5)
        m20, ir20, w20, n20d = stat(ic20)

        # 去重：与现有湖逐列池化 Spearman（秩相关，抽样日）
        nd = df.copy()
        nd["date"] = pd.to_datetime(nd["date"])
        nd = nd[nd["date"].isin(sample_dates["date"])]
        nd = nd.rename(columns={"value": "v"}).set_index(["date", "code"])["v"]
        max_corr, max_name = 0.0, ""
        joined = wide_lake.join(nd, how="inner")
        if len(joined) > 1000:
            r = joined.rank()
            corrs = r.corrwith(r["v"]).drop("v").dropna()
            if len(corrs):
                max_corr = float(corrs.abs().max())
                max_name = str(corrs.abs().idxmax())
        results.append({
            "factor": name,
            "ic5": m5, "icir5": ir5, "win5": w5,
            "ic20": m20, "icir20": ir20, "win20": w20,
            "icir20_2026": yearly(ic20).get("2026"),
            "icir20_2025": yearly(ic20).get("2025"),
            "icir20_2024": yearly(ic20).get("2024"),
            "icir20_2023": yearly(ic20).get("2023"),
            "icir20_2022": yearly(ic20).get("2022"),
            "max_abs_corr": round(max_corr, 3),
            "max_corr_vs": max_name,
            "n_days20": n20d,
        })
        print(f"  ic20={m20:+.4f} icir20={ir20:+.2f} max|ρ|={max_corr:.2f}({max_name})",
              flush=True)

    res = pd.DataFrame(results)
    out = Path("reports/phase1_screen.csv")
    res.to_csv(out, index=False, encoding="utf-8-sig")
    print("\n" + res.to_string(index=False))
    print(f"\n已保存 {out}")


if __name__ == "__main__":
    main()
