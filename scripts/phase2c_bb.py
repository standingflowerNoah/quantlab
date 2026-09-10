#!/usr/bin/env python3
"""Phase 2c · 多空博弈因子族（方正之十三，2026-09-10）
======================================================================
研报公式（RankIC -9.73% / ICIR -5.51，负向）：
  日频（逐分钟，剔除开盘前5分钟+收盘前3分钟）：
    dvr  = Σ_t v_t·(n+1−2·rank(t))，rank 按「过去5分钟收益率
           close_t/close_{t-5}−1」升序（稳定排序）——成交量博弈-收益率
    dvp  = 同上，rank 按「日内相对位置」升序。相对位置 = mean(
           (close−running_min_low)/running_min_low, (running_max_high−close)/running_max_high)
           ——成交量博弈-日内相对位置
    damp = 同上，rank 按 r5 升序，v 换成分钟振幅 (high−low)/close —— 振幅博弈
  月度合成（全市场截面）：
    ① 均值距离化：日频值截面 z-score 后取绝对值
    ② 月均 = 20 日滚动均值；月稳 = 20 日滚动标准差；等权合成子因子
    ③ 成交量博弈 = (成交量博弈-收益率 + 成交量博弈-日内相对位置) 等权
    ④ 多空博弈 = (成交量博弈 + 振幅博弈) 等权
工程：分钟湖逐月批次 + 月度检查点断点续算（同 phase2_minute.py 模式）。
产出 4 个候选：bb_volret_20 / bb_volpos_20 / bb_ampl_20 / bb_composite_20
（均为「越激烈越差」负向因子，入湖即按原始方向存。）
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


def month_iter():
    yield from [
        (2025, 1, "2025-01-01", "2025-01-31"),
        (2025, 2, "2025-02-01", "2025-02-28"),
        (2025, 3, "2025-03-01", "2025-03-31"),
        (2025, 4, "2025-04-01", "2025-04-30"),
        (2025, 5, "2025-05-01", "2025-05-31"),
        (2025, 6, "2025-06-01", "2025-06-30"),
        (2025, 7, "2025-07-01", "2025-07-31"),
        (2025, 8, "2025-08-01", "2025-08-31"),
        (2025, 9, "2025-09-01", "2025-09-30"),
        (2025, 10, "2025-10-01", "2025-10-31"),
        (2025, 11, "2025-11-01", "2025-11-30"),
        (2025, 12, "2025-12-01", "2025-12-31"),
        (2026, 1, "2026-01-01", "2026-01-31"),
        (2026, 2, "2026-02-01", "2026-02-28"),
        (2026, 3, "2026-03-01", "2026-03-31"),
        (2026, 4, "2026-04-01", "2026-04-30"),
        (2026, 5, "2026-05-01", "2026-05-31"),
        (2026, 6, "2026-06-01", "2026-06-30"),
        (2026, 7, "2026-07-01", "2026-07-31"),
        (2026, 8, "2026-08-01", "2026-08-31"),
    ]


def month_bb(files, y, m, s, e):
    import duckdb
    con = duckdb.connect()
    df = con.execute(f"""
        SELECT code, CAST(datetime AS DATE) AS d, datetime, vol AS volume,
               close, high, low
        FROM read_parquet([{files}])
        WHERE CAST(datetime AS DATE) BETWEEN DATE '{s}' AND DATE '{e}'
          AND close > 0 AND vol >= 0
        ORDER BY code, datetime
    """).df()
    con.close()
    if df.empty:
        return pd.DataFrame()
    print(f"  {y}-{m:02d}: {len(df):,} 分钟 bars", flush=True)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["tod"] = df["datetime"].dt.strftime("%H:%M")
    # 剔除开盘前 5 分钟（09:30-09:34 bar）与收盘前 3 分钟（14:58-15:00）
    df = df[(df["tod"] >= "09:35") & (df["tod"] <= "14:57")].copy()
    df = df.drop(columns=["tod"])
    g = df.groupby(["code", "d"], sort=False)
    n_min = g["volume"].transform("size")
    df = df[n_min >= 120].copy()          # 有效分钟数门槛
    g = df.groupby(["code", "d"], sort=False)
    # 过去 5 分钟收益率（组内 shift）
    df["r5"] = g["close"].transform(lambda s: s / s.shift(5) - 1)
    df["amp"] = (df["high"] - df["low"]) / df["close"]
    # 日内相对位置：running min low / max high（组内 cummin/cummax）
    rl = g["low"].cummin()
    rh = g["high"].cummax()
    pos = (df["close"] - rl) / rl.replace(0, np.nan)
    pos2 = (rh - df["close"]) / rh.replace(0, np.nan)
    df["pos"] = (pos + pos2) / 2
    df = df.dropna(subset=["r5"])
    g = df.groupby(["code", "d"], sort=False)
    n = g["volume"].transform("size")
    df = df[n >= 100].copy()
    g = df.groupby(["code", "d"], sort=False)
    # 秩加权：d = Σ v·(n+1−2·rank_asc)，稳定排序 rank(method=first)
    rk_r5 = g["r5"].rank(method="first")
    rk_pos = g["pos"].rank(method="first")
    df["n"] = g["volume"].transform("size")
    df["w_r5"] = df["n"] + 1 - 2 * rk_r5
    df["w_pos"] = df["n"] + 1 - 2 * rk_pos
    agg = df.assign(
        dvr=df["volume"] * df["w_r5"],
        dvp=df["volume"] * df["w_pos"],
        damp=df["amp"] * df["w_r5"],
    )
    res = agg.groupby(["code", "d"])[["dvr", "dvp", "damp"]].sum().reset_index()
    res["d"] = pd.to_datetime(res["d"]).dt.date
    return res


def main():
    import duckdb
    ckpt_dir = Path("data/_phase2c_bb")
    ckpt_dir.mkdir(exist_ok=True)
    for y, m, s, e in month_iter():
        ck = ckpt_dir / f"{y}-{m:02d}.parquet"
        if ck.exists():
            continue
        files = ", ".join(
            "'" + p.replace("\\", "/") + "'"
            for p in sorted(glob.glob(f"data/lake/clean/kline_1min/year={y}/part-*.parquet")))
        df_m = month_bb(files, y, m, s, e)
        for c in ("dvr", "dvp", "damp"):
            df_m[c] = df_m[c].astype("float32")
        df_m.to_parquet(ck, index=False)
        print(f"checkpoint {ck.name}: {len(df_m):,} code-days", flush=True)
    ev = pd.concat([pd.read_parquet(ck) for ck in sorted(ckpt_dir.glob("*.parquet"))],
                   ignore_index=True)
    ev = ev.sort_values(["code", "d"]).reset_index(drop=True)
    print(f"合并 {len(ev):,} code-days（{ev['d'].nunique()} 交易日）", flush=True)

    # ── 月度合成 ──────────────────────────────────────────────
    # ① 均值距离化（当日截面 z-score 取绝对值）
    for c in ("dvr", "dvp", "damp"):
        mu = ev.groupby("d")[c].transform("mean")
        sd = ev.groupby("d")[c].transform("std")
        ev[f"z{c}"] = ((ev[c] - mu) / sd.replace(0, np.nan)).abs()
    # ② 20 日滚动均值/标准差（按 code 时序）
    ev["d"] = pd.to_datetime(ev["d"])
    ev = ev.sort_values(["code", "d"])
    g = ev.groupby("code")
    for c in ("zdvr", "zdvp", "zdamp"):
        ev[f"m_{c}"] = g[c].transform(lambda s: s.rolling(20, min_periods=10).mean())
        ev[f"s_{c}"] = g[c].transform(lambda s: s.rolling(20, min_periods=10).std())
    # ③ 子因子 = 月均与月稳的截面 rank 等权（缺失跳过）
    def cross_rank(s):
        return s.groupby(ev["d"]).rank(pct=True)
    ev["bb_volret_20"] = (cross_rank(ev["m_zdvr"]) + cross_rank(ev["s_zdvr"])) / 2
    ev["bb_volpos_20"] = (cross_rank(ev["m_zdvp"]) + cross_rank(ev["s_zdvp"])) / 2
    ev["bb_ampl_20"] = (cross_rank(ev["m_zdamp"]) + cross_rank(ev["s_zdamp"])) / 2
    ev["bb_composite_20"] = (ev["bb_volret_20"] + ev["bb_volpos_20"]) / 2 * 0.5 \
        + ev["bb_ampl_20"] * 0.5
    factors = ["bb_volret_20", "bb_volpos_20", "bb_ampl_20", "bb_composite_20"]

    # ── IC 快筛 + 相关闸 ─────────────────────────────────────
    con = duckdb.connect()
    fwd = con.execute(f"""
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
    exist = sorted(p.name for p in (ROOT / "data/lake/factor").iterdir()
                   if p.is_dir())
    for f in factors:
        sub = ev[["code", "d", f]].dropna().merge(fwd[["d", "code", "ret20"]],
                                                  on=["d", "code"])
        if sub.empty:
            continue
        ic = sub.groupby("d").apply(
            lambda x: x[f].rank().corr(x["ret20"].rank()),
            include_groups=False).dropna()
        sub["year"] = sub["d"].dt.year
        yic = {int(y): round(v, 3) for y, v in
               sub.groupby("year").apply(
                   lambda x: x[f].rank().corr(x["ret20"].rank()),
                   include_groups=False).dropna().items()}
        # 相关闸：与现有 284 因子 rank 相关（抽样同日）
        from quantlab.data.store import Store
        store = Store(readonly=True)
        max_corr, max_vs = 0.0, ""
        try:
            sample_d = sorted(sub["d"].unique())[::20]
            for other in exist:
                try:
                    ov = store.read_factor(other)
                except Exception:
                    continue
                if ov.empty:
                    continue
                ov["d"] = pd.to_datetime(ov["d"])
                ov = ov[ov["d"].isin(sample_d)]
                if ov.empty:
                    continue
                mg = sub[sub["d"].isin(sample_d)][["d", "code", f]].merge(
                    ov, on=["d", "code"])
                if len(mg) < 10000:
                    continue
                rho = abs(mg[f].rank().corr(mg["value"].rank()))
                if rho > max_corr:
                    max_corr, max_vs = rho, other
        except Exception as e:
            print(f"  相关闸异常 {f}: {e}", flush=True)
        rows.append({"factor": f, "ic20": round(ic.mean(), 4),
                     "icir20": round(ic.mean() / ic.std(), 3),
                     "n_days": len(ic), "yearly_ic": yic,
                     "max_abs_corr": round(max_corr, 3),
                     "max_corr_vs": max_vs})
        print(f"{f}: ic={ic.mean():+.4f} icir={ic.mean()/ic.std():+.2f} "
              f"max|rho|={max_corr:.2f}({max_vs})", flush=True)

    res = pd.DataFrame(rows)
    res.to_csv("reports/phase2c_bb_screen.csv", index=False, encoding="utf-8-sig")
    print("\n" + res.to_string(index=False))

    # ── 入湖（|ρ|≤0.9 闸）+ 审计 + 清理检查点 ────────────────
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
    for r in rows:
        name = r["factor"]
        if r["max_abs_corr"] > 0.9:
            print(f"{name}: |ρ|={r['max_abs_corr']:.2f} > 0.9，不入湖", flush=True)
            continue
        sub = ev[["code", "d", name]].rename(
            columns={"d": "date", name: "value"}).dropna()
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
