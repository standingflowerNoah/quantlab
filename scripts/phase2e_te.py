#!/usr/bin/env python3
"""Phase 2e · 一视同仁因子（方正之十八，2026-09-11）
======================================================================
研报公式（RankIC -7.39% / ICIR -4.09，负向）：
  逐日逐股（剔除开盘前5分钟/收盘前3分钟，剔除零成交量分钟）：
  ① 成交量 Box-Cox 变换（逐日逐股网格 MLE，λ∈[-0.9,1.5] 步长 0.15）
  ② Δv = 变换后成交量的相邻差分；mean/std 后：
     正态激增时刻 = Δv > mean+std；正态骤降时刻 = Δv < mean−std
  ③ 「波动公平」日值 = |耀眼波动率 − 黯淡波动率| × 当日日内收益
     「收益公平」日值 = |耀眼收益率 − 黯淡收益率| × 当日日内收益
     （耀眼/黯淡五分钟 = 激增/骤降时刻及其后 4 分钟；波动率=窗口分钟收益标准差，
       收益率=窗口复合收益）
  ④ 各取过去 20 日均值 → 两子因子 rank 等权 → 一视同仁（越小越好）
工程：分钟湖逐月批次 + 月度检查点断点续算。
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
WIN = 5
LAMBDAS = np.round(np.arange(-0.9, 1.5 + 1e-9, 0.15), 2)


def month_iter():
    out = []
    for y, m, d_end in [(2025, 1, 31), (2025, 2, 28), (2025, 3, 31), (2025, 4, 30),
                        (2025, 5, 31), (2025, 6, 30), (2025, 7, 31), (2025, 8, 31),
                        (2025, 9, 30), (2025, 10, 31), (2025, 11, 30), (2025, 12, 31),
                        (2026, 1, 31), (2026, 2, 28), (2026, 3, 31), (2026, 4, 30),
                        (2026, 5, 31), (2026, 6, 30), (2026, 7, 31), (2026, 8, 31)]:
        out.append((y, m, f"{y}-{m:02d}-01", f"{y}-{m:02d}-{d_end:02d}"))
    return out


def boxcox_arr(x, lam):
    if abs(lam) < 1e-6:
        return np.log(x)
    return (np.power(x, lam) - 1.0) / lam


def group_fairness(v, c):
    """单只股票单日：返回 (波动公平日值, 收益公平日值) 或 (nan, nan)"""
    n = len(v)
    if n < 60:
        return np.nan, np.nan
    x = v.astype("float64")
    logx = np.log(x)
    # 网格 MLE 选 λ（profile likelihood）
    best_ll, best_lam = -np.inf, None
    for lam in LAMBDAS:
        y = boxcox_arr(x, lam)
        sd = y.std()
        if sd <= 0:
            continue
        ll = -n / 2 * np.log(sd ** 2) + (lam - 1) * logx.sum()
        if ll > best_ll:
            best_ll, best_lam = ll, lam
    if best_lam is None:
        return np.nan, np.nan
    tv = boxcox_arr(x, best_lam)
    dv = np.diff(tv)
    if len(dv) < 20:
        return np.nan, np.nan
    mu, sd = dv.mean(), dv.std()
    if sd <= 0:
        return np.nan, np.nan
    surge = np.where(dv > mu + sd)[0]
    drop = np.where(dv < mu - sd)[0]
    if len(surge) == 0 or len(drop) == 0:
        return np.nan, np.nan
    # 分钟收益与窗口统计（cumsum 技巧）
    cc = c.astype("float64")
    r = np.zeros(n)
    r[1:] = cc[1:] / cc[:-1] - 1
    cs = np.concatenate([[0.0], np.cumsum(r)])
    cs2 = np.concatenate([[0.0], np.cumsum(r ** 2)])
    def win_stats(idx_arr):
        vols, rets = [], []
        for i in idx_arr:
            j = min(i + WIN, n)
            if j - i < WIN:
                continue
            cnt = j - i
            mr = (cs[j] - cs[i]) / cnt
            vr = (cs2[j] - cs2[i]) / cnt - mr ** 2
            vols.append(np.sqrt(max(vr, 0.0)))
            if cc[i] > 0:
                rets.append(cc[j - 1] / cc[i] - 1)
        if not vols or not rets:
            return np.nan, np.nan
        return float(np.mean(vols)), float(np.mean(rets))
    sv, sr = win_stats(surge)
    dv_, dr = win_stats(drop)
    if np.isnan(sv) or np.isnan(dv_):
        return np.nan, np.nan
    day_ret = cc[-1] / cc[0] - 1
    vol_fair = abs(sv - dv_) * day_ret
    ret_fair = (abs(sr - dr) * day_ret
                if not (np.isnan(sr) or np.isnan(dr)) else np.nan)
    return vol_fair, ret_fair


def month_te(files, y, m, s, e):
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
    df["tod"] = df["datetime"].dt.strftime("%H:%M")
    df = df[(df["tod"] >= "09:35") & (df["tod"] <= "14:57")]
    df = df[df["vol"] > 0]
    rows = []
    for (code, day), g in df.groupby(["code", "d"], sort=False):
        vf, rf = group_fairness(g["vol"].to_numpy(), g["close"].to_numpy())
        if np.isnan(vf) and np.isnan(rf):
            continue
        rows.append({"code": code, "d": day, "vol_fair": vf, "ret_fair": rf})
    res = pd.DataFrame(rows)
    if res.empty:
        return res
    res["d"] = pd.to_datetime(res["d"]).dt.date
    return res


def main():
    ckpt_dir = Path("data/_phase2e_te")
    ckpt_dir.mkdir(exist_ok=True)
    for y, m, s, e in month_iter():
        ck = ckpt_dir / f"{y}-{m:02d}.parquet"
        if ck.exists():
            continue
        files = ", ".join(
            "'" + p.replace("\\", "/") + "'"
            for p in sorted(glob.glob(f"data/lake/clean/kline_1min/year={y}/part-*.parquet")))
        df_m = month_te(files, y, m, s, e)
        if df_m.empty:
            print(f"  {y}-{m:02d}: 无有效样本", flush=True)
            continue
        for c in ("vol_fair", "ret_fair"):
            df_m[c] = df_m[c].astype("float32")
        df_m.to_parquet(ck, index=False)
        print(f"checkpoint {ck.name}: {len(df_m):,} code-days", flush=True)
    ev = pd.concat([pd.read_parquet(ck) for ck in sorted(ckpt_dir.glob("*.parquet"))],
                   ignore_index=True)
    ev["d"] = pd.to_datetime(ev["d"])
    ev = ev.sort_values(["code", "d"]).reset_index(drop=True)
    print(f"合并 {len(ev):,} code-days（{ev['d'].nunique()} 交易日）", flush=True)

    g = ev.groupby("code")
    ev["vol_fair_20"] = g["vol_fair"].transform(
        lambda s: s.rolling(20, min_periods=10).mean())
    ev["ret_fair_20"] = g["ret_fair"].transform(
        lambda s: s.rolling(20, min_periods=10).mean())
    ev["treat_equal_20"] = (
        ev.groupby("d")["vol_fair_20"].rank(pct=True)
        + ev.groupby("d")["ret_fair_20"].rank(pct=True)) / 2
    factors = ["vol_fair_20", "ret_fair_20", "treat_equal_20"]
    ev_out = ev[["code", "d"] + factors].dropna(subset=["treat_equal_20"])

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
    for r in rows:
        name = r["factor"]
        d = ev_out[["code", "d"]].copy()
        d["value"] = ev_out[name].values
        sample = d.dropna()
        recent = sorted(sample["d"].unique())[-60:]
        sample = sample[sample["d"].isin(recent)]
        best, bname = 0.0, ""
        for o in exist:
            if o == name or o in factors:
                continue
            try:
                ov = store.read_factor(o, start=recent[0])
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
        if name != "treat_equal_20" and r["max_abs_corr"] > 0.9:
            continue
        sub = ev_out[["code", "d"]].copy()
        sub["value"] = ev_out[name].values
        sub = sub.dropna().rename(columns={"d": "date"})
        sub = sub[np.isfinite(sub["value"])]
        if sub.empty:
            continue
        n = store.append_factor(name, sub)
        rec = audit_factor(name, factor=None, df=sub, deep=True)
        print(f"{name}: 入湖 {n} 行, 审计 {rec['verdict']}", flush=True)

    pd.DataFrame(rows).to_csv("reports/phase2e_te_screen.csv", index=False,
                              encoding="utf-8-sig")
    for ck in ckpt_dir.glob("*.parquet"):
        ck.unlink()
    ckpt_dir.rmdir()
    print("检查点已清理", flush=True)


if __name__ == "__main__":
    main()
