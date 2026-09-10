#!/usr/bin/env python3
"""Phase 2d · 花隐林间因子（方正之十，2026-09-11）
======================================================================
研报公式（RankIC -9.34% / ICIR -5.69，负向）：
  逐日逐股 OLS：r_t = α + β1·Δv_{t-1} + … + β5·Δv_{t-5} + ε
    （Δv = 分钟成交量的一阶差分；样本 = 当日全部有效分钟）
  ① 朝没晨雾 = std(β1..β5 的 t 值)，过去 20 日均值（越小越好：无突发信息）
  ② 午蔽古木 = |t_α|，按当日截面 F 统计量中位数翻转符号（F<中位 → 乘 −1），
     过去 20 日均值（越小越好：噪声少）
  ③ 夜眠霜路 = 个股过去 20 日 t_α 序列与全市场 t_α 日均值序列的 |corr|
     （越大越好：中长期基本面分歧小）
  合成：三子截面 rank 等权 → 整体越小越好
工程：分钟湖逐月批次 + 月度检查点断点续算（同 phase2_minute.py / phase2c 模式）。
产出：flower_forest_20 入湖（+ 三子因子同时入湖供诊断）。
"""
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import glob

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
LAG = 5
MIN_MINUTES = 150          # 当日有效分钟门槛


def month_iter():
    out = []
    for y, m, d_end in [(2025, 1, 31), (2025, 2, 28), (2025, 3, 31), (2025, 4, 30),
                        (2025, 5, 31), (2025, 6, 30), (2025, 7, 31), (2025, 8, 31),
                        (2025, 9, 30), (2025, 10, 31), (2025, 11, 30), (2025, 12, 31),
                        (2026, 1, 31), (2026, 2, 28), (2026, 3, 31), (2026, 4, 30),
                        (2026, 5, 31), (2026, 6, 30), (2026, 7, 31), (2026, 8, 31)]:
        out.append((y, m, f"{y}-{m:02d}-01", f"{y}-{m:02d}-{d_end:02d}"))
    return out


def month_flower(files, y, m, s, e):
    import duckdb
    con = duckdb.connect()
    df = con.execute(f"""
        SELECT code, CAST(datetime AS DATE) AS d, datetime, close, vol
        FROM read_parquet([{files}])
        WHERE CAST(datetime AS DATE) BETWEEN DATE '{s}' AND DATE '{e}'
          AND close > 0
        ORDER BY code, datetime
    """).df()
    con.close()
    if df.empty:
        return pd.DataFrame()
    print(f"  {y}-{m:02d}: {len(df):,} 分钟 bars", flush=True)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["r"] = df.groupby("code", sort=False)["close"].transform(
        lambda x: x.pct_change())
    # 增量成交量 Δv
    df["dv"] = df.groupby("code", sort=False)["vol"].transform(
        lambda x: x.diff())
    for lag in range(1, LAG + 1):
        df[f"dv{lag}"] = df.groupby("code", sort=False)["dv"].shift(lag)
    cols = ["r"] + [f"dv{lag}" for lag in range(1, LAG + 1)]
    df = df.dropna(subset=cols)
    # 只保留同一交易日内完整的记录
    df = df[df["dv5"].notna()]

    results = []
    grp = df.groupby(["code", "d"], sort=False)
    for (code, day), g in grp:
        n = len(g)
        if n < MIN_MINUTES:
            continue
        y_ = g["r"].to_numpy(dtype="float64")
        X = np.column_stack([np.ones(n)] + [g[f"dv{lag}"].to_numpy(dtype="float64")
                                            for lag in range(1, LAG + 1)])
        try:
            XtX = X.T @ X
            Xty = X.T @ y_
            beta = np.linalg.solve(XtX, Xty)
            resid = y_ - X @ beta
            ssr = float(resid @ resid)
            k = X.shape[1]
            dof = n - k
            if dof <= 0 or ssr <= 0:
                continue
            sigma2 = ssr / dof
            inv = np.linalg.inv(XtX)
            se = np.sqrt(np.maximum(sigma2 * np.diag(inv), 1e-300))
            tvals = beta / se
            sst = float(((y_ - y_.mean()) ** 2).sum())
            r2 = 1 - ssr / sst if sst > 0 else np.nan
            f_all = (r2 / LAG) / ((1 - r2) / dof) if 0 < r2 < 1 else np.nan
        except np.linalg.LinAlgError:
            continue
        results.append({
            "code": code, "d": day,
            "t_int": float(tvals[0]),
            "morning_mist": float(np.std(tvals[1:])),   # 朝没晨雾（日值）
            "f_all": f_all,
            "open_mu": float(y_.mean()),                # 日内收益（夜眠霜路用）
        })
    res = pd.DataFrame(results)
    if res.empty:
        return res
    res["d"] = pd.to_datetime(res["d"]).dt.date
    return res


def main():
    ckpt_dir = Path("data/_phase2d_flower")
    ckpt_dir.mkdir(exist_ok=True)
    for y, m, s, e in month_iter():
        ck = ckpt_dir / f"{y}-{m:02d}.parquet"
        if ck.exists():
            continue
        files = ", ".join(
            "'" + p.replace("\\", "/") + "'"
            for p in sorted(glob.glob(f"data/lake/clean/kline_1min/year={y}/part-*.parquet")))
        df_m = month_flower(files, y, m, s, e)
        if df_m.empty:
            print(f"  {y}-{m:02d}: 无有效样本", flush=True)
            continue
        for c in ("t_int", "morning_mist", "f_all", "open_mu"):
            df_m[c] = df_m[c].astype("float32")
        df_m.to_parquet(ck, index=False)
        print(f"checkpoint {ck.name}: {len(df_m):,} code-days", flush=True)
    ev = pd.concat([pd.read_parquet(ck) for ck in sorted(ckpt_dir.glob("*.parquet"))],
                   ignore_index=True)
    ev["d"] = pd.to_datetime(ev["d"])
    ev = ev.sort_values(["code", "d"]).reset_index(drop=True)
    print(f"合并 {len(ev):,} code-days（{ev['d'].nunique()} 交易日）", flush=True)

    # ── 三子因子 ─────────────────────────────────────────────
    g = ev.groupby("code")
    # ① 朝没晨雾：20 日均值（越小越好，负向）
    ev["morning_mist_20"] = g["morning_mist"].transform(
        lambda s: s.rolling(20, min_periods=10).mean())
    # ② 午蔽古木：|t_int| 按当日 F 中位数翻转 → 20 日均值
    med_f = ev.groupby("d")["f_all"].transform("median")
    ev["obscured"] = np.where(ev["f_all"] < med_f, -ev["t_int"].abs(),
                              ev["t_int"].abs())
    ev["obscured_wood_20"] = ev.groupby("code")["obscured"].transform(
        lambda s: s.rolling(20, min_periods=10).mean())
    # ③ 夜眠霜路：个股 20 日 t_int 序列 vs 全市场 t_int 日均值序列的 |corr|
    mkt = ev.groupby("d")["t_int"].mean().rename("mkt_t")
    ev = ev.merge(mkt, left_on="d", right_index=True, how="left")
    def corr_abs(s, m):
        s = s.to_numpy(dtype="float64")
        m = m.to_numpy(dtype="float64")
        if np.isnan(s).any() or np.isnan(m).any() or np.std(s) == 0 or np.std(m) == 0:
            return np.nan
        return abs(np.corrcoef(s, m)[0, 1])
    # 为控制耗时，按 20 日窗口滚动（用 rolling.apply）
    ev = ev.sort_values(["code", "d"]).reset_index(drop=True)
    def _frost(gg):
        n = len(gg)
        vals = [np.nan] * n
        ti = gg["t_int"].to_numpy(dtype="float64")
        mk = gg["mkt_t"].to_numpy(dtype="float64")
        for i in range(19, n):
            s, m_ = ti[i - 19:i + 1], mk[i - 19:i + 1]
            if np.isnan(s).any() or np.isnan(m_).any():
                continue
            if np.std(s) == 0 or np.std(m_) == 0:
                continue
            vals[i] = abs(np.corrcoef(s, m_)[0, 1])
        return pd.Series(vals, index=gg.index)
    ev["frost_road_20"] = ev.groupby("code", group_keys=False).apply(
        _frost, include_groups=False).astype("float32")

    # ── 合成（三子 rank 等权；方向：晨雾↓、古木↓、霜路↑） ──
    def cr(s_col, ascending=True):
        r = ev.groupby("d")[s_col].rank(pct=True)
        return r if ascending else (1 - r)
    comp = (cr("morning_mist_20") + cr("obscured_wood_20")
            + cr("frost_road_20", ascending=False)) / 3
    ev["flower_forest_20"] = comp
    factors = ["morning_mist_20", "obscured_wood_20", "frost_road_20",
               "flower_forest_20"]
    ev_out = ev[["code", "d"] + factors].dropna(subset=["flower_forest_20"])

    # ── IC 快筛 ──────────────────────────────────────────────
    import duckdb
    con = duckdb.connect()
    fwd = con.execute("""
        SELECT CAST(date AS DATE) AS d, code,
               LEAD(close * adj_factor, 1) OVER p AS c1,
               LEAD(close * adj_factor, 21) OVER p AS c21
        FROM read_parquet('data/lake/clean/mirror/kline_daily.parquet')
        WINDOW p AS (PARTITION BY code ORDER BY date)
    """).df()
    fwd["ret20"] = fwd["c21"] / fwd["c1"] - 1
    fwd = fwd.dropna(subset=["ret20"])
    fwd["d"] = pd.to_datetime(fwd["d"])
    rows = []
    for f in factors:
        sub = ev_out[["code", "d", f]].dropna().merge(
            fwd[["d", "code", "ret20"]], on=["d", "code"])
        if sub.empty:
            continue
        ic = sub.groupby("d").apply(
            lambda x: x[f].rank().corr(x["ret20"].rank()),
            include_groups=False).dropna()
        sub["year"] = sub["d"].dt.year
        yic = {int(y): round(v, 3) for y, v in sub.groupby("year").apply(
            lambda x: x[f].rank().corr(x["ret20"].rank()),
            include_groups=False).dropna().items()}
        rows.append({"factor": f, "ic20": round(ic.mean(), 4),
                     "icir20": round(ic.mean() / ic.std(), 3),
                     "n_days": len(ic), "yearly_ic": yic})
        print(f"{f}: ic={ic.mean():+.4f} icir={ic.mean()/ic.std():+.2f} "
              f"days={len(ic)} yearly={yic}", flush=True)

    res = pd.DataFrame(rows)
    res.to_csv("reports/phase2d_flower_screen.csv", index=False,
               encoding="utf-8-sig")
    print("\n" + res.to_string(index=False))

    # ── 相关闸（0.9）+ 入湖 ──────────────────────────────────
    from pathlib import Path as P
    from quantlab.data.store import Store
    from quantlab.factor.audit import audit_factor
    store = None
    for i in range(120):
        try:
            store = Store()
            break
        except Exception:
            if i == 0:
                print("等待 DuckDB 写锁...", flush=True)
            time.sleep(60)
    exist = sorted(p.name for p in (ROOT / "data/lake/factor").iterdir()
                   if p.is_dir())
    pass_ = []
    for r in rows:
        name = r["factor"]
        d = ev_out[["code", "d"]].copy()
        d["value"] = ev_out[name].values
        sample = d.dropna()
        # 相关闸抽样：近期 60 个交易日截面（控成本，同 phase2c 做法）
        recent_days = sorted(sample["d"].unique())[-60:]
        sample = sample[sample["d"].isin(recent_days)]
        best, bname = 0.0, ""
        for o in exist:
            if o == name or o in factors:
                continue
            try:
                ov = store.read_factor(o, start=recent_days[0])
            except Exception:
                continue
            if ov.empty or "value" not in ov.columns:
                continue
            ov = ov.copy()
            ov["date"] = pd.to_datetime(ov["date"])
            mg = sample.rename(columns={"d": "date"}).merge(
                ov, on=["date", "code"], suffixes=("_a", "_b"))
            if len(mg) < 50000:
                continue
            rho = abs(mg["value_a"].rank().corr(mg["value_b"].rank()))
            if rho > best:
                best, bname = rho, o
        r["max_abs_corr"], r["max_corr_vs"] = round(best, 3), bname
        print(f"{name}: max|rho|={best:.3f} vs {bname}", flush=True)

    for r in rows:
        name = r["factor"]
        if name != "flower_forest_20" and r["max_abs_corr"] > 0.9:
            print(f"{name}: |ρ|>0.9 不入湖", flush=True)
            continue
        sub = ev_out[["code", "d"]].copy()
        sub["value"] = ev_out[name].values
        sub = sub.dropna().rename(columns={"d": "date"})
        sub = sub[np.isfinite(sub["value"])]
        n = store.append_factor(name, sub)
        rec = audit_factor(name, factor=None, df=sub, deep=True)
        print(f"{name}: 入湖 {n} 行, 审计 {rec['verdict']}", flush=True)

    res2 = pd.DataFrame(rows)
    res2.to_csv("reports/phase2d_flower_screen.csv", index=False,
                encoding="utf-8-sig")
    for ck in ckpt_dir.glob("*.parquet"):
        ck.unlink()
    ckpt_dir.rmdir()
    print("检查点已清理", flush=True)


if __name__ == "__main__":
    main()
