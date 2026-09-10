#!/usr/bin/env python3
"""Phase 2：方正系列分钟真口径因子（2026-09-10）
=====================================
本批 4 组公式已按研报原文核实（多因子选股系列之一/二/八/十一）：
1) 完整潮汐：邻域成交量(±4分钟和)顶峰→涨潮时刻/退潮时刻，
   段内价格变动速率；强势半潮汐=终点更低的一端，完整潮汐=两段20日均值和
2) 适度冒险：成交量增量>mean+std 为激增时刻，耀眼5分钟收益std（耀眼波动率）
   与激增时刻分钟收益（耀眼收益率），日值减截面均值取绝对值，20日均值/标准差
3) 草木皆兵（波动率加剧版）：惊恐度=|r−rm|/(|r|+|rm|+0.1)，
   加权决策分=日收益×惊恐度×分钟收益std，20日均值/标准差
4) 待著而救：09:45 后成交量 Top10 分钟→间隔>5分钟筛为优势时刻，
   跟随系数=后5分钟成交量/优势时刻成交量，日均值→20日均值/标准差

近似口径（文档已标注）：日内分钟序列用本库分钟湖（2025+）；
市场收益=全市场个股日收益均值（研报用中证全指）。

用法：python scripts/phase2_minute.py
"""
import glob
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import duckdb
import numpy as np
import pandas as pd

WIN = 20
MIN_MINUTES = 120      # 日内有效分钟门槛
MIN_DAYS = 10          # 20日窗口有效日门槛


def month_iter():
    for y in (2025, 2026):
        for m in range(1, 13):
            start = f"{y:04d}-{m:02d}-01"
            if start >= "2026-09-01":
                continue
            end = (f"{y + 1}-01-01" if m == 12 else f"{y:04d}-{m + 1:02d}-01")
            yield y, m, start, end


def tide_of(c: np.ndarray, v: np.ndarray):
    """完整潮汐：返回 (全潮汐日值, 强势段速率, 弱势段速率) 或 None"""
    n = len(v)
    if n < 30:
        return None
    nv = np.convolve(v, np.ones(9), "valid")       # nv[i]=sum(v[i:i+9])，对应分钟 i+4
    if len(nv) < 3:
        return None
    t = int(np.argmax(nv))
    if t == 0 or t == len(nv) - 1:
        return None
    mi = int(np.argmin(nv[:t]))                    # 涨潮时刻（nv 索引）
    ni = t + 1 + int(np.argmin(nv[t + 1:]))        # 退潮时刻
    T, M, N = t + 4, mi + 4, ni + 4                # 分钟数组索引
    if not (N > T > M):
        return None
    all_tide = (c[N] - c[M]) / c[M] / (N - M)
    v_m, v_n = nv[mi], nv[ni]
    if v_m > v_n:   # 退潮段强势
        strong = (c[N] - c[T]) / c[T] / (N - T)
        weak = (c[T] - c[M]) / c[M] / (T - M)
    else:
        strong = (c[T] - c[M]) / c[M] / (T - M)
        weak = (c[N] - c[T]) / c[T] / (N - T)
    return all_tide, strong, weak


def glare_of(c: np.ndarray, v: np.ndarray):
    """适度冒险日代理：返回 (日耀眼波动率, 日耀眼收益率) 或 None"""
    n = len(v)
    if n < 30:
        return None
    r = c[1:] / c[:-1] - 1                          # r[i] 对应分钟 i+1
    dv = np.diff(v, prepend=v[0])
    sd = dv.std()
    if sd <= 0:
        return None
    surge = np.where(dv > dv.mean() + sd)[0]
    surge = surge[(surge >= 1) & (surge <= n - 2)]
    if len(surge) == 0:
        return None
    stds, rets = [], []
    for s in surge:
        seg = r[s: min(s + 5, len(r))]              # 耀眼5分钟（近似）
        if len(seg) >= 2:
            stds.append(seg.std())
        rets.append(r[s - 1])                       # 激增时刻的分钟收益
    if not stds:
        return None
    return float(np.mean(stds)), float(np.mean(rets))


def wait_of(c: np.ndarray, v: np.ndarray, times: np.ndarray):
    """待著而救日跟随系数（09:45 后），返回 float 或 None"""
    tod = times - times.astype("datetime64[D]")     # 当日时间部分
    keep = tod >= np.timedelta64(9 * 3600 + 45 * 60, "s")
    v2, t2 = v[keep], times[keep]
    if len(v2) < 60:
        return None
    idx = np.argsort(v2)[::-1][:10]                 # 海量时刻
    idx = np.sort(idx)
    adv = []
    last = -10
    for i in idx:
        if i - last > 5:
            adv.append(i)
            last = i
    coefs = []
    for s in adv:
        if s + 5 < len(v2) and v2[s] > 0:
            coefs.append(v2[s + 1:s + 6].sum() / v2[s])
    if not coefs:
        return None
    return float(np.mean(coefs))


def month_proxies(files: str, y: int, m: int, start: str, end: str) -> pd.DataFrame:
    """单月：逐 (code, day) 计算日度代理变量"""
    con = duckdb.connect()
    try:
        df = con.execute(f"""
            SELECT code, CAST(datetime AS DATE) AS d, datetime, close, vol
            FROM read_parquet([{files}])
            WHERE close > 0 AND vol > 0
              AND datetime >= TIMESTAMP '{start}' AND datetime < TIMESTAMP '{end}'
            ORDER BY code, datetime
        """).df()
    finally:
        con.close()
    if df.empty:
        return pd.DataFrame()
    rows = []
    for (code, d), g in df.groupby(["code", "d"], sort=False):
        c = g["close"].to_numpy()
        v = g["vol"].to_numpy()
        if len(c) < MIN_MINUTES:
            continue
        r = c[1:] / c[:-1] - 1
        rec = {"code": code, "d": d, "dayret": c[-1] / c[0] - 1,
               "vol_m": float(r.std())}
        td = tide_of(c, v)
        if td:
            rec["tide_all"], rec["tide_strong"], rec["tide_weak"] = td
        gl = glare_of(c, v)
        if gl:
            rec["glare_vol"], rec["glare_ret"] = gl
        tv = g["datetime"].to_numpy()
        w = wait_of(c, v, tv)
        if w is not None:
            rec["wait_d"] = w
        rows.append(rec)
    out = pd.DataFrame(rows)
    print(f"  {y}-{m:02d}: {len(out):,} code-days", flush=True)
    return out


def main():
    # 分批断点续算：逐月计算日度代理 → data/_phase2_proxies/ 月度检查点
    # （float32，每月 ~2-4MB）。重跑跳过已完成月份；合并后内存算滚动统计。
    ckpt_dir = Path("data/_phase2_proxies")
    ckpt_dir.mkdir(exist_ok=True)
    for y, m, s, e in month_iter():
        ck = ckpt_dir / f"{y}-{m:02d}.parquet"
        if ck.exists():
            continue
        files = ", ".join(
            "'" + p.replace("\\", "/") + "'"
            for p in sorted(glob.glob(f"data/lake/clean/kline_1min/year={y}/part-*.parquet")))
        df_m = month_proxies(files, y, m, s, e)
        for c in ("dayret", "vol_m", "tide_all", "tide_strong", "tide_weak",
                  "glare_vol", "glare_ret", "wait_d"):
            if c in df_m:
                df_m[c] = df_m[c].astype("float32")
        df_m.to_parquet(ck, index=False)
        print(f"checkpoint {ck.name}: {len(df_m):,} rows", flush=True)
    ev = pd.concat([pd.read_parquet(ck) for ck in sorted(ckpt_dir.glob("*.parquet"))],
                   ignore_index=True)
    print(f"合并 {len(ev):,} code-days（{ev['d'].nunique()} 交易日）", flush=True)
    print(f"合计 {len(ev):,} code-days", flush=True)

    # 截面调整
    mkt = ev.groupby("d")["dayret"].mean().rename("mktret")
    ev = ev.join(mkt, on="d")
    gv = ev.groupby("d")["glare_vol"].transform("mean")
    gr = ev.groupby("d")["glare_ret"].transform("mean")
    ev["mod_vol_d"] = (ev["glare_vol"] - gv).abs()
    ev["mod_ret_d"] = (ev["glare_ret"] - gr).abs()
    panic = (ev["dayret"] - ev["mktret"]).abs() / (
        ev["dayret"].abs() + ev["mktret"].abs() + 0.1)
    ev["grass_w"] = ev["dayret"] * panic * ev["vol_m"]

    # 20 日滚动统计
    ev = ev.sort_values(["code", "d"])
    gb = ev.groupby("code", group_keys=False)

    def roll(col, fn):
        return gb[col].apply(lambda s: getattr(s.rolling(WIN, min_periods=MIN_DAYS),
                                               fn)())

    ev["tide_all_20"] = roll("tide_all", "mean")
    ev["tide_half_20"] = (roll("tide_strong", "mean").fillna(0)
                          + roll("tide_weak", "mean").fillna(0))
    ev.loc[ev["tide_strong"].isna() & ev["tide_weak"].isna(), "tide_half_20"] = np.nan
    for base, nm in (("mod_vol_d", "mod_vol"), ("mod_ret_d", "mod_ret"),
                     ("grass_w", "grass"), ("wait_d", "wait")):
        ev[f"{nm}_m20"] = roll(base, "mean")
        ev[f"{nm}_s20"] = roll(base, "std")

    def zcomb(g: pd.DataFrame) -> pd.Series:
        """同日截面 z 合成：z(mean) + z(std)"""
        zm = (g - g.mean()) / (g.std() if g.std() > 0 else 1)
        return zm

    dts = ev["d"]
    ev["moderate_risk_20"] = (zcomb(ev["mod_vol_m20"]) + zcomb(ev["mod_vol_s20"])
                              + zcomb(ev["mod_ret_m20"]) + zcomb(ev["mod_ret_s20"]))
    ev["panic_grass_20"] = zcomb(ev["grass_m20"]) + zcomb(ev["grass_s20"])
    ev["waitsave_20"] = zcomb(ev["wait_m20"]) + zcomb(ev["wait_s20"])
    # 分组因子也保留单列
    ev["mod_risk_vol_20"] = zcomb(ev["mod_vol_m20"]) + zcomb(ev["mod_vol_s20"])
    ev["mod_risk_ret_20"] = zcomb(ev["mod_ret_m20"]) + zcomb(ev["mod_ret_s20"])
    del dts

    factors = ["tide_all_20", "tide_half_20", "moderate_risk_20",
               "mod_risk_vol_20", "mod_risk_ret_20", "panic_grass_20", "waitsave_20"]

    # IC 快筛
    from quantlab.data.store import Store
    from quantlab.factor.audit import audit_factor
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

    sample = pd.DataFrame({"date": sorted(fwd["date"].unique())[-250:]}).iloc[::12]
    con.register("sample_sd", sample)
    lake_names = [x.name for x in Path("data/lake/factor").iterdir()
                  if x.is_dir() and x.name not in factors]
    unions = []
    for name in lake_names:
        globs = [str(p).replace("\\", "/")
                 for p in Path(f"data/lake/factor/{name}").glob("part-*.parquet")
                 if any(yy in p.name for yy in ("2024", "2025", "2026"))]
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
    for name in factors:
        sub = ev[["code", "d", name]].rename(columns={"d": "date", name: "value"}).dropna()
        sub["date"] = pd.to_datetime(sub["date"])
        m = sub.merge(fwd, on=["date", "code"], how="inner")
        m = m[np.isfinite(m["value"]) & np.isfinite(m["fwd"])]
        ics = [gg["value"].rank().corr(gg["fwd"].rank())
               for _, gg in m.groupby("date") if len(gg) >= 30]
        ic = pd.Series(ics)
        icir = ic.mean() / ic.std() if ic.std() > 0 else float("nan")
        m["_y"] = m["date"].dt.year
        yearly = {int(y): round(float(mm["value"].rank().corr(mm["fwd"].rank())), 3)
                  for y, mm in m.groupby("_y") if len(mm) >= 30}
        nd = sub[sub["date"].isin(sample["date"])]
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
                     "n_days": len(ic), "yearly_ic": yearly,
                     "max_abs_corr": round(max_corr, 3), "max_corr_vs": max_name})
        print(f"{name}: ic20={ic.mean():+.4f} icir={icir:+.2f} "
              f"max|rho|={max_corr:.2f}({max_name})", flush=True)

    res = pd.DataFrame(rows)
    res.to_csv("reports/phase2_screen.csv", index=False, encoding="utf-8-sig")
    print("\n" + res.to_string(index=False))

    # 入湖（|ρ|≤0.9 闸）+ 清理检查点
    from quantlab.factor.audit import audit_factor
    for r in rows:
        name = r["factor"]
        if r["max_abs_corr"] > 0.9:
            print(f"{name}: |ρ|={r['max_abs_corr']:.2f} > 0.9，不入湖", flush=True)
            continue
        sub = ev[["code", "d", name]].rename(columns={"d": "date", name: "value"}).dropna()
        sub = sub[np.isfinite(sub["value"])]
        n = store.append_factor(name, sub)
        rec = audit_factor(name, factor=None, df=sub, deep=True)
        print(f"{name}: 入湖 {n} 行, 审计 {rec['verdict']} issues={rec['issues']}",
              flush=True)
    for ck in ckpt_dir.glob("*.parquet"):
        ck.unlink()
    ckpt_dir.rmdir()
    print("检查点已清理", flush=True)


if __name__ == "__main__":
    main()
