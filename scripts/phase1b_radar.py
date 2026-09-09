#!/usr/bin/env python3
"""异动雷达事件簇（国盛量价淘金十六）分钟级计算 + 快筛（2026-09-10）
=====================================
研报框架（日频因子化）：
- 异动判定：日内个股与基准的分钟序列相关系数 < 0（研报：纯价格维度无效、
  量/资金流维度有效——基准用全市场逐分钟均值近似，资金流维度用成交额占比代理）
- 方向拆分：当日个股收益 vs 市场均值收益 → 上涨异动 / 下跌异动
- 因子 = 过去 20 交易日事件计数：radar_up_20 / radar_down_20 / radar_vol_20

工程：in-process 分年处理分钟湖（2025+），file-backed scratch duckdb（可落盘溢出），
      结束后删除 scratch。写锁惯例：不触碰 data/quant.duckdb 写路径。

用法：python scripts/phase1b_radar.py
"""
import glob
import os
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import duckdb
import numpy as np
import pandas as pd

SCRATCH = "data/_radar_tmp.duckdb"
YEARS = (2025, 2026)
MIN_N = 180          # (code, day) 有效分钟数门槛（240 分钟全日）
WIN = 20             # 事件计数窗口
MIN_EV_DAYS = 10     # 窗口内有效日门槛


def year_files(y: int) -> str:
    fs = sorted(glob.glob(f"data/lake/clean/kline_1min/year={y}/part-*.parquet"))
    return ", ".join("'" + f.replace("\\", "/") + "'" for f in fs)


def month_pred(y: int, m: int) -> tuple[str, str]:
    start = f"{y:04d}-{m:02d}-01"
    if m == 12:
        end = f"{y + 1:04d}-01-01"
    else:
        end = f"{y:04d}-{m + 1:02d}-01"
    return start, end


def per_month_corr(files: str, y: int, m: int) -> pd.DataFrame:
    """单月：逐 (code, day) 计算相关成分 + 日收益（相关全在日内，跨月切分无损）。

    用全新 in-memory con，杜绝磁盘溢出（C 盘 100% 满的教训）。
    """
    start, end = month_pred(y, m)
    con = duckdb.connect()  # 每月一个全新内存库，用完即释放
    try:
        sql = f"""
        WITH m AS (
            SELECT code, CAST(datetime AS DATE) AS d, datetime, close, amount
            FROM read_parquet([{files}])
            WHERE close > 0 AND amount > 0
              AND datetime >= TIMESTAMP '{start}' AND datetime < TIMESTAMP '{end}'
        ),
        r AS (
            SELECT code, d, datetime, close, amount,
                   close / LAG(close) OVER (PARTITION BY code, d ORDER BY datetime) - 1 AS rr
            FROM m
        ),
        mk AS (
            SELECT datetime, AVG(rr) AS mr FROM r WHERE rr IS NOT NULL GROUP BY datetime
        ),
        msum AS (
            SELECT datetime, SUM(amount) AS a FROM r GROUP BY datetime
        ),
        mday AS (
            SELECT CAST(datetime AS DATE) AS d, SUM(a) AS da
            FROM msum GROUP BY CAST(datetime AS DATE)
        ),
        j AS (
            SELECT r.code, r.d, r.datetime, r.close, r.rr, r.amount,
                   k.mr, ms.a / md.da AS mvs
            FROM r
            JOIN mk k USING (datetime)
            JOIN msum ms USING (datetime)
            JOIN mday md ON md.d = r.d
            WHERE r.rr IS NOT NULL
        ),
        s AS (
            SELECT code, d, datetime, close, rr, mr,
                   amount / SUM(amount) OVER (PARTITION BY code, d) AS vs,
                   mvs
            FROM j
        ),
        g AS (
            SELECT code, d,
                   COUNT(rr) AS n,
                   ARG_MIN(close, datetime) AS c0,
                   ARG_MAX(close, datetime) AS c1,
                   AVG(rr) AS ar, AVG(mr) AS amr, AVG(rr * mr) AS armr,
                   STDDEV_SAMP(rr) AS sr, STDDEV_SAMP(mr) AS smr,
                   AVG(vs) AS av, AVG(mvs) AS amv, AVG(vs * mvs) AS vmv,
                   STDDEV_SAMP(vs) AS sv, STDDEV_SAMP(mvs) AS smv
            FROM s
            GROUP BY code, d
        )
        SELECT code, d, n, c1 / c0 - 1 AS dayret,
               (armr - ar * amr) / NULLIF(sr * smr, 0) AS cr,
               (vmv - av * amv) / NULLIF(sv * smv, 0) AS cv
        FROM g
        WHERE n >= {MIN_N // 3}
        """
        df = con.execute(sql).df()
    finally:
        con.close()
    df["d"] = pd.to_datetime(df["d"])
    print(f"  {y}-{m:02d}: {len(df):,} (code,day) 组", flush=True)
    return df


def main():
    all_files = ", ".join(
        "'" + p.replace("\\", "/") + "'"
        for yy in YEARS
        for p in sorted(glob.glob(f"data/lake/clean/kline_1min/year={yy}/part-*.parquet")))
    frames = []
    for y in YEARS:
        for m in range(1, 13):
            start, _ = month_pred(y, m)
            if start >= "2026-09-01":   # 数据到 2026-09-08，9 月不完整且不足 20 日回看
                continue
            frames.append(per_month_corr(all_files, y, m))

    ev = pd.concat(frames, ignore_index=True)
    del frames
    ev = ev[ev["n"] >= MIN_N].copy()

    # 市场日收益 = 当日全市场个股日收益均值；超额方向
    mkt = ev.groupby("d")["dayret"].mean().rename("mktret")
    ev = ev.join(mkt, on="d")
    ev["ev_up"] = ((ev["cr"] < 0) & (ev["dayret"] > ev["mktret"])).astype(int)
    ev["ev_dn"] = ((ev["cr"] < 0) & (ev["dayret"] <= ev["mktret"])).astype(int)
    ev["ev_v"] = (ev["cv"] < 0).astype(int)
    print(f"事件统计：上涨异动 {ev['ev_up'].sum():,} / 下跌异动 {ev['ev_dn'].sum():,} "
          f"/ 成交异动 {ev['ev_v'].sum():,}（共 {len(ev):,} 股·日）", flush=True)

    # 20 日事件计数（ROWS 窗口）
    ev = ev.sort_values(["code", "d"])
    gb = ev.groupby("code", group_keys=False)
    for col, name in (("ev_up", "radar_up_20"), ("ev_dn", "radar_down_20"),
                      ("ev_v", "radar_vol_20")):
        ev[name] = gb[col].apply(
            lambda s: s.rolling(WIN, min_periods=MIN_EV_DAYS).sum())
    ev["n_day"] = gb["dayret"].apply(
        lambda s: s.rolling(WIN, min_periods=1).count())

    # 快筛：IC20（前复权 fwd）
    from quantlab.data.store import Store
    store = Store()
    kline = store.q("SELECT date, code, close * adj_factor AS c FROM kline_daily")
    con = duckdb.connect()
    con.register("kd", kline)
    fwd = con.execute(f"""
        SELECT date, code, c_lead / c - 1 AS fwd FROM (
            SELECT CAST(date AS DATE) AS date, code, c,
                   LEAD(c, {WIN}) OVER (PARTITION BY code ORDER BY date) AS c_lead
            FROM kd
        ) WHERE c_lead IS NOT NULL AND c > 0
    """).df()
    fwd["date"] = pd.to_datetime(fwd["date"])

    # 去重基座（2024+ 分区抽样）
    sample = pd.DataFrame({"date": sorted(fwd["date"].unique())[-250:]})
    sample = sample.iloc[::12]
    con.register("sample_sd", sample)
    lake_names = [x.name for x in Path("data/lake/factor").iterdir()
                  if x.is_dir() and not x.name.startswith("radar_")]
    unions = []
    for name in lake_names:
        globs = [str(p2).replace("\\", "/")
                 for p2 in Path(f"data/lake/factor/{name}").glob("part-*.parquet")
                 if any(yy in p2.name for yy in ("2024", "2025", "2026"))]
        if globs:
            paths = ", ".join(f"'{x}'" for x in globs)
            unions.append(f"SELECT '{name}' AS f, CAST(date AS DATE) AS date, "
                          f"code, CAST(value AS REAL) AS v FROM read_parquet([{paths}])")
    long_df = con.execute("SELECT * FROM (" + " UNION ALL ".join(unions) + ") "
                          "WHERE date IN (SELECT date FROM sample_sd) "
                          "AND v IS NOT NULL").df()
    wide = long_df.pivot_table(index=["date", "code"], columns="f",
                               values="v", aggfunc="first").reset_index()
    wide["date"] = pd.to_datetime(wide["date"]).dt.normalize()
    wide = wide.set_index(["date", "code"])
    del long_df

    rows = []
    for name in ("radar_up_20", "radar_down_20", "radar_vol_20"):
        sub = ev[["code", "d", name, "n_day"]].dropna()
        sub = sub[sub["n_day"] >= MIN_EV_DAYS]
        sub = sub.rename(columns={"d": "date", name: "value"})
        vals = sub[["date", "code", "value"]]
        m = vals.merge(fwd, on=["date", "code"], how="inner")
        m = m[np.isfinite(m["value"]) & np.isfinite(m["fwd"])]
        ics = []
        for d, gg in m.groupby("date"):
            if len(gg) < 30:
                continue
            ics.append(gg["value"].rank().corr(gg["fwd"].rank()))
        ic = pd.Series(ics)
        icir = ic.mean() / ic.std() if ic.std() > 0 else float("nan")
        m["_y"] = pd.to_datetime(m["date"]).dt.year
        yearly = {}
        for y, mm in m.groupby("_y"):
            if len(mm) >= 30:
                yearly[int(y)] = round(
                    float(mm["value"].rank().corr(mm["fwd"].rank())), 3)
        nd = sub.copy()
        nd["date"] = pd.to_datetime(nd["date"]).dt.normalize()
        nd = nd[nd["date"].isin(sample["date"])]
        ndi = nd.set_index(["date", "code"])["value"]
        joined = wide.join(ndi.rename("nv"), how="inner")
        max_corr, max_name = 0.0, ""
        if len(joined) > 1000:
            r = joined.rank()
            corrs = r.corrwith(r["nv"]).drop("nv").dropna()
            if len(corrs):
                max_corr = float(corrs.abs().max())
                max_name = str(corrs.abs().idxmax())
        rows.append({"factor": name, "ic20": round(float(ic.mean()), 4),
                     "icir20": round(float(icir), 3),
                     "win": round(float((ic > 0).mean()), 3),
                     "n_days": len(ic),
                     "yearly_ic": yearly,
                     "max_abs_corr": round(max_corr, 3),
                     "max_corr_vs": max_name})
        print(f"{name}: ic20={ic.mean():+.4f} icir={icir:+.2f} "
              f"max|rho|={max_corr:.2f}({max_name})", flush=True)

    res = pd.DataFrame(rows)
    res.to_csv("reports/phase1_radar_screen.csv", index=False,
               encoding="utf-8-sig")
    out_cols = ["code", "d", "n_day", "radar_up_20", "radar_down_20", "radar_vol_20"]
    ev[out_cols].to_parquet("reports/_radar_values.parquet", index=False)
    print("\n" + res.to_string(index=False))
    print("已保存 reports/phase1_radar_screen.csv 与 reports/_radar_values.parquet")


if __name__ == "__main__":
    main()
