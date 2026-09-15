# -*- coding: utf-8 -*-
"""hf 批次 R1 鲁棒性：分段 IC + leave-one-year-out + 市场状态 + placebo
=====================================================================
评价框架 R1 缺口落地（research/factor_eval_dimensions_20260912.md §2），
全部无锁（metric_store L0 + 因子湖 + mirror/kline_daily）：

  1) 年度分段（h20）        rank IC 逐年 mean/ICIR/win、反号计数
  2) leave-one-year-out     剔除单年后 ICIR 最差值（报告最差而非均值）
  3) 半年度连续性           相邻半年段 IC 同号比例
  4) 市场状态依赖           等权市场 120 日滚动收益分上行/下行态，两态 ICIR
  5) placebo 截面置换       月末截面 × 500 次行内 shuffle，真实 |IC| 的经验 p

判定规则（先定后跑）：
  🟢 LOCO 反号=0 且 |worst ICIR|≥0.5|full| 且 半年同号≥0.6 且 两态同号 且 p<0.05
  🔴 LOCO 反号≥2 或 p≥0.2
  🟡 其余

用法：python scripts/hf_r1_robustness.py [--perms 500] [--factors a,b]
产出：reports/_tmp/hf_r1_robustness_summary.csv
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
METRIC_GLOB = str(ROOT / "data/lake/factor_metric_daily/ashare_ex/part-*.parquet")
MIRROR = str(ROOT / "data/lake/clean/mirror/kline_daily.parquet")
FACTOR_DIR = ROOT / "data/lake/factor"
OUT_CSV = ROOT / "reports/_tmp/hf_r1_robustness_summary.csv"
EVAL_CSV = ROOT / "reports/_tmp/hf_hist_eval_summary.csv"

CON = duckdb.connect()
CON.execute("SET memory_limit='2GB'")
CON.execute("SET threads=4")
CON.execute("SET preserve_insertion_order=false")

PERMS = 500
MIN_SEG_DAYS = 15        # 半年段最少 IC 日
MIN_XSEC = 30            # placebo 截面最少股票数


# ---------------------------------------------------------------- 数据层
def load_regime() -> pd.DataFrame:
    """等权市场状态：日收益中位数 → 120 日滚动均值（shift 1 防当日前视）>0=上行。"""
    return CON.execute(f"""
        WITH px AS (
            SELECT date, code, close * adj_factor AS c,
                   LAG(close * adj_factor) OVER (
                       PARTITION BY code ORDER BY date) AS c_prev
            FROM read_parquet('{MIRROR}')
        ),
        day AS (
            SELECT date, median(c / c_prev - 1) AS mret
            FROM px WHERE c_prev > 0 GROUP BY date
        )
        SELECT date,
               CASE WHEN avg(mret) OVER (
                        ORDER BY date ROWS BETWEEN 120 PRECEDING AND 1 PRECEDING
                    ) > 0 THEN 'up' ELSE 'down' END AS regime
        FROM day ORDER BY date
    """).fetchdf()


def load_daily_ic() -> pd.DataFrame:
    df = CON.execute(f"""
        SELECT factor, date, rank_ic5, rank_ic20, n_codes
        FROM read_parquet('{METRIC_GLOB}')
        WHERE factor LIKE 'hf_%' ORDER BY factor, date
    """).fetchdf()
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_fwd20() -> pd.DataFrame:
    return CON.execute(f"""
        SELECT date, code, c20 / c - 1 AS fwd FROM (
            SELECT date, code, close * adj_factor AS c,
                   LEAD(close * adj_factor, 20) OVER (
                       PARTITION BY code ORDER BY date) AS c20
            FROM read_parquet('{MIRROR}'))
        WHERE c20 IS NOT NULL AND c > 0
    """).fetchdf()


def month_end_dates(dates: pd.Series) -> list[pd.Timestamp]:
    s = dates.drop_duplicates().sort_values()
    key = s.dt.to_period("M")
    return list(s.groupby(key).max())


# ------------------------------------------------------------- 分段统计
def _ic_stats(s: pd.Series) -> tuple[float, float, float]:
    s = s.dropna()
    if len(s) < 2 or s.std() == 0:
        return (np.nan, np.nan, np.nan)
    return (float(s.mean()), float(s.mean() / s.std()), float((s > 0).mean()))


def segment_stats(ic: pd.Series, dates: pd.Series, freq: str) -> pd.DataFrame:
    df = pd.DataFrame({"date": dates, "ic": ic}).dropna()
    if freq == "Y":
        df["seg"] = df["date"].dt.year
    else:
        df["seg"] = (df["date"].dt.year.astype(str)
                     + np.where(df["date"].dt.month <= 6, "H1", "H2"))
    g = df.groupby("seg")["ic"]
    out = pd.DataFrame({
        "n": g.size(),
        "ic": g.mean(),
        "icir": g.mean() / g.std(),
        "win": g.apply(lambda x: (x > 0).mean()),
    })
    return out[out["n"] >= MIN_SEG_DAYS]


def loco_stats(ic: pd.Series, dates: pd.Series, full_icir: float) -> dict:
    yr = pd.Series(dates).dt.year
    rows = []
    for y in sorted(yr.unique()):
        m = (yr != y).to_numpy()
        if (~m).sum() < 100:
            continue
        _, icir, _ = _ic_stats(ic[m])
        rows.append({"drop": int(y), "icir": icir})
    worst = None
    n_flip = 0
    for r in rows:
        if np.isfinite(r["icir"]) and np.isfinite(full_icir):
            if np.sign(r["icir"]) != np.sign(full_icir):
                n_flip += 1
            if worst is None or abs(r["icir"]) < abs(worst):
                worst = r["icir"]
    return {"loco_worst_icir": worst, "loco_n_flip": n_flip,
            "loco_detail": rows}


# ---------------------------------------------------------------- placebo
def placebo_pvalue(fv: pd.DataFrame, fwd: pd.DataFrame,
                   me_dates: list, rng: np.random.Generator) -> dict:
    """月末截面行内 shuffle 的经验 p（|placebo IC| ≥ |真实 IC| 的比例）。"""
    m = fv.merge(fwd, on=["date", "code"], how="inner").dropna(
        subset=["value", "fwd"])
    m["date"] = pd.to_datetime(m["date"]).dt.normalize()
    m["date"] = m["date"].astype("datetime64[ns]")
    m = m[m["date"].isin(set(me_dates))]
    if m.empty:
        return {"p": np.nan, "real_ic_me": np.nan, "n_sections": 0}
    m["vr"] = m.groupby("date")["value"].rank()
    m["fr"] = m.groupby("date")["fwd"].rank()
    reals, ps = [], []
    for d, g in m.groupby("date"):
        if len(g) < MIN_XSEC:
            continue
        a = g["vr"].to_numpy(float)
        b = g["fr"].to_numpy(float)
        real = float(np.corrcoef(a, b)[0, 1])
        idx = np.argsort(rng.random((PERMS, len(a))), axis=1)
        A = a[idx]
        Am = A - A.mean(axis=1, keepdims=True)
        bm = b - b.mean()
        num = Am @ bm
        den = np.sqrt((Am ** 2).sum(axis=1) * (bm ** 2).sum())
        pc = num / den
        reals.append(real)
        ps.append(float((np.abs(pc) >= abs(real)).mean()))
    if not reals:
        return {"p": np.nan, "real_ic_me": np.nan, "n_sections": 0}
    return {"p": float(np.mean(ps)), "real_ic_me": float(np.mean(reals)),
            "n_sections": len(reals)}


# ---------------------------------------------------------------- 主流程
def main() -> None:
    global PERMS
    ap = argparse.ArgumentParser()
    ap.add_argument("--perms", type=int, default=PERMS)
    ap.add_argument("--factors", help="逗号分隔子集")
    a = ap.parse_args()
    PERMS = a.perms

    t0 = time.time()
    factors = ([s.strip() for s in a.factors.split(",")] if a.factors
               else sorted(p.name for p in FACTOR_DIR.glob("hf_*") if p.is_dir()))
    fdr = {}
    if EVAL_CSV.exists():
        ev = pd.read_csv(EVAL_CSV)
        if "FDR_q" in ev.columns:
            ev["FDR_q"] = pd.to_numeric(
                ev["FDR_q"].astype(str).str.replace(r"[^0-9.]", "", regex=True),
                errors="coerce")
            fdr = dict(zip(ev["factor"], ev["FDR_q"]))

    print("加载市场状态 / 日 IC 序列 / fwd20 ...", flush=True)
    regime = load_regime()
    regime["date"] = pd.to_datetime(regime["date"]).dt.normalize()
    regime = regime.set_index("date")["regime"]
    daily = load_daily_ic()
    fwd = load_fwd20()
    print(f"  日IC {len(daily):,} 行 / fwd20 {len(fwd):,} 行 "
          f"({time.time()-t0:.0f}s)", flush=True)

    rng = np.random.default_rng(42)
    rows = []
    for i, fac in enumerate(factors, 1):
        sub = daily[daily["factor"] == fac]
        ic20 = pd.Series(sub["rank_ic20"].values,
                         index=pd.DatetimeIndex(sub["date"]))
        ic20 = ic20.dropna()
        full_ic, full_icir, full_win = _ic_stats(ic20)
        yr = segment_stats(ic20, ic20.index, "Y")
        n_flip_y = int((np.sign(yr["ic"]) != np.sign(full_ic)).sum())
        hy = segment_stats(ic20, ic20.index, "H")
        sign = np.sign(hy["ic"])
        sign = sign[sign != 0]
        hy_cont = float((sign == sign.shift(-1)).iloc[:-1].mean()
                        ) if len(sign) > 1 else np.nan
        reg = pd.DataFrame({"date": ic20.index, "ic": ic20.values})
        reg["regime"] = reg["date"].map(regime)
        st = {r: _ic_stats(g["ic"])[1] for r, g in reg.groupby("regime")}
        st_agree = (np.sign(st.get("up", np.nan))
                    == np.sign(st.get("down", np.nan)))
        lo = loco_stats(ic20, ic20.index, full_icir)
        fv = CON.execute(f"""
            SELECT CAST(date AS DATE) AS date, code, value
            FROM read_parquet('{(FACTOR_DIR / fac).as_posix()}/*.parquet')
        """).fetchdf()
        pv = placebo_pvalue(fv, fwd, month_end_dates(fv["date"]), rng)
        worst = lo["loco_worst_icir"]
        amp_ok = (np.isfinite(worst) and np.isfinite(full_icir)
                  and abs(worst) >= 0.5 * abs(full_icir))
        if lo["loco_n_flip"] >= 2 or (np.isfinite(pv["p"]) and pv["p"] >= 0.2):
            verdict = "🔴"
        elif (lo["loco_n_flip"] == 0 and amp_ok and hy_cont >= 0.6
              and st_agree and np.isfinite(pv["p"]) and pv["p"] < 0.05):
            verdict = "🟢"
        else:
            verdict = "🟡"
        rows.append({
            "factor": fac,
            "FDR_q": fdr.get(fac, np.nan),
            "survivor": bool(np.isfinite(fdr.get(fac, np.nan))
                             and fdr.get(fac, 1) < 0.25),
            "full_ic20": round(full_ic, 4),
            "full_icir20": round(full_icir, 4),
            "yr_flip": n_flip_y,
            "yr_icir_min": round(float(yr["icir"].abs().min()), 3),
            "loco_worst_icir": round(worst, 3) if np.isfinite(worst) else np.nan,
            "loco_n_flip": lo["loco_n_flip"],
            "hy_cont": round(hy_cont, 3) if np.isfinite(hy_cont) else np.nan,
            "icir_up": round(st.get("up", np.nan), 3),
            "icir_down": round(st.get("down", np.nan), 3),
            "regime_agree": bool(st_agree),
            "placebo_p": round(pv["p"], 4) if np.isfinite(pv["p"]) else np.nan,
            "real_ic_me": round(pv["real_ic_me"], 4),
            "me_sections": pv["n_sections"],
            "R1_verdict": verdict,
        })
        print(f"[{i}/{len(factors)}] {fac}: {verdict} "
              f"loco_flip={lo['loco_n_flip']} worst={worst:.3f} "
              f"hy={hy_cont:.2f} p={pv['p']:.3f} ({time.time()-t0:.0f}s)",
              flush=True)

    out = pd.DataFrame(rows)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\n汇总 → {OUT_CSV}")
    print(out[["factor", "survivor", "loco_n_flip", "loco_worst_icir",
               "hy_cont", "regime_agree", "placebo_p", "R1_verdict"]]
          .to_string(index=False))


if __name__ == "__main__":
    main()
