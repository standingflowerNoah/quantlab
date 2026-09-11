#!/usr/bin/env python3
"""P4：基本面中性化组合 vs 生产模型 A/B（统一回测口径 + 3 相位）
=====================================================================
Case:
  PROD      = size+amihud_20 等权 top100（生产，基准）
  FUND4     = size中性化 cfp_ttm+np_q_yoy+rev_q_yoy+ocf_to_profit 等权 top100
              （P3b 中 IS/OOS 双稳的现金流+成长族）
  PROD+FUND4= size+amihud_20+fund4 三段等权（增强组）
  CFP单腿   = 中性化 cfp_ttm 单因子 top100（最强单腿对照）

中性化：每日 factor_rank ~ size_rank OLS 残差 → 残差 pct rank（×方向）。
统一口径：top100 / 20 日调仓 / inverse_vol / 全成本 / 基准中证1000。
相位：start 偏移 0/5/10 交易日（红利评估同款 3 相位闸门）。

输出: reports/fundamental_factor/ab_test.csv + ab_phases.csv
"""
from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
FACTOR_DIR = ROOT / "data/lake/factor"
OUT_DIR = ROOT / "reports/fundamental_factor"
OUT_DIR.mkdir(parents=True, exist_ok=True)
DB = str(ROOT / "data/quant.duckdb").replace("\\", "/")

FULL_START = "2022-07-01"
END = "2026-09-04"
REBAL = 20

con = duckdb.connect()
con.execute(f"ATTACH '{DB}' AS q (READ_ONLY)")

con.execute("""
CREATE TEMP TABLE fwd20 AS
SELECT date, code, c_lead / c - 1 AS fwd
FROM (
    SELECT date, code, close * adj_factor AS c,
           LEAD(close * adj_factor, 20) OVER (
               PARTITION BY code ORDER BY date) AS c_lead
    FROM q.kline_daily)
WHERE c_lead IS NOT NULL AND c > 0
""")

from quantlab.data.universe import get_universe
UNI = set(get_universe("ashare_ex"))

t0 = time.time()
px = con.execute("""
    SELECT CAST(date AS DATE) AS date, code, close * adj_factor AS c
    FROM q.kline_daily
""").fetchdf()
px["date"] = pd.to_datetime(px["date"])
PMAT = px.pivot(index="date", columns="code", values="c").sort_index()
con.execute("DETACH q")
print(f"pmat {PMAT.shape} ({time.time()-t0:.0f}s)", flush=True)

# —— size 原始秩 ——
_size_files = sorted((FACTOR_DIR / "size").glob("part-*.parquet"))
_size = con.execute(f"""
    SELECT CAST(date AS DATE) AS date, code, value
    FROM read_parquet([{", ".join("'" + str(f).replace("\\", "/") + "'" for f in _size_files)}])
""").fetchdf()
_size["date"] = pd.to_datetime(_size["date"])
_size = _size[np.isfinite(pd.to_numeric(_size["value"], errors="coerce"))]
_size["srank"] = _size.groupby("date")["value"].rank(pct=True)
SRANK = _size[["date", "code", "srank"]]


def load_ranked(name: str, direction: int = 1) -> pd.DataFrame:
    files = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    paths = ", ".join("'" + str(f).replace("\\", "/") + "'" for f in files)
    df = con.execute(f"""
        SELECT CAST(date AS DATE) AS date, code, value
        FROM read_parquet([{paths}])
    """).fetchdf()
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df[np.isfinite(df["value"]) & df["code"].isin(UNI)]
    df["r"] = df.groupby("date")["value"].rank(pct=True) * direction
    return df[["date", "code", "r"]]


def neutralize(df: pd.DataFrame) -> pd.DataFrame:
    """每日 factor_rank ~ size_rank OLS 残差 → 残差 pct rank"""
    m = df.merge(SRANK, on=["date", "code"], how="inner")
    out = []
    for d, g in m.groupby("date"):
        if len(g) < 300:
            continue
        x = g["srank"].values
        y = g["r"].values
        A = np.column_stack([x, np.ones_like(x)])
        beta, *_ = np.linalg.lstsq(A, y, rcond=None)
        res = y - A @ beta
        out.append(pd.DataFrame({"date": d, "code": g["code"].values,
                                 "r": pd.Series(res).rank(pct=True).values}))
    return pd.concat(out, ignore_index=True)


def build_composite(frames: list[pd.DataFrame]) -> pd.DataFrame:
    allf = pd.concat(frames, ignore_index=True)
    return (allf.groupby(["date", "code"])["r"].mean()
            .reset_index().rename(columns={"r": "score"}))


def ic_series(score: pd.DataFrame) -> pd.Series:
    """逐日 Rank IC20（复用 fwd20）"""
    sc = score.copy()
    sc["date"] = pd.to_datetime(sc["date"]).dt.date
    con.register("score_df", sc)
    df = con.execute("""
        WITH j AS (
            SELECT CAST(s.date AS DATE) AS date, s.code, s.score, w.fwd
            FROM score_df s JOIN fwd20 w
              ON CAST(s.date AS DATE) = w.date AND s.code = w.code
            WHERE s.score IS NOT NULL),
        rk AS (
            SELECT date, score, fwd,
                   rank() OVER (PARTITION BY date ORDER BY score) AS rv,
                   rank() OVER (PARTITION BY date ORDER BY fwd) AS rf,
                   count(*) OVER (PARTITION BY date) AS n
            FROM j),
        rk2 AS (SELECT * FROM rk WHERE n >= 300)
        SELECT date, corr(rv, rf) AS ic FROM rk2 GROUP BY date ORDER BY date
    """).fetchdf()
    ic = df["ic"].dropna()
    ic.index = pd.to_datetime(df["date"]).iloc[:len(ic)]
    return ic


def tail_metrics(score: pd.DataFrame) -> dict:
    fwd = con.execute("SELECT CAST(date AS DATE) AS date, code, fwd FROM fwd20").fetchdf()
    fwd["date"] = pd.to_datetime(fwd["date"])
    fwd = fwd[fwd["code"].isin(UNI)]
    fwd_idx = fwd.set_index(["date", "code"])["fwd"]
    uni_mean = fwd_idx.groupby(level=0).mean()
    sr_idx = SRANK.set_index(["date", "code"])["srank"]

    sc = score.sort_values(["date", "score"], ascending=[True, False])
    dates = sorted(sc["date"].unique())
    dates = [d for d in dates if d >= pd.Timestamp(FULL_START)]
    rb = dates[::REBAL]
    spreads, sranks = [], []
    for d0 in rb:
        top = sc[sc["date"] == d0].head(100)
        if len(top) < 100 or d0 not in uni_mean.index:
            continue
        f = fwd_idx.loc[d0]
        vals = f.reindex(top["code"]).dropna()
        if len(vals) < 80:
            continue
        spreads.append(vals.mean() - uni_mean.loc[d0])
        sranks.append(sr_idx.loc[d0].reindex(top["code"]).mean())
    sp = pd.Series(spreads, index=rb[:len(spreads)]).dropna()
    return {
        "tail_spread_ann": round(float(sp.mean() * 252 / REBAL), 4),
        "tail_win": round(float((sp > 0).mean()), 3),
        "size_rank_holdings": round(float(np.nanmean(sranks)), 4),
    }


def standalone_bt(score: pd.DataFrame, start: str = FULL_START) -> dict:
    from quantlab.optimize.backtest import run_optimized_backtest
    r = run_optimized_backtest(score.copy(), n_stocks=100, rebalance=REBAL,
                               method="inverse_vol", start=start,
                               end=END, pmat=PMAT)
    m = r["metrics"]
    return {"annual": round(m["annual_return"], 4),
            "sharpe": round(m["sharpe"], 3),
            "mdd": round(m["max_drawdown"], 4),
            "turnover": r["turnover_avg"]}


# ── 构造各 case ──────────────────────────────────────────────
print("构造评分…", flush=True)
sz = load_ranked("size", -1)
am = load_ranked("amihud_20", 1)
score_prod = build_composite([sz, am])

FUND4 = ["cfp_ttm", "np_q_yoy", "rev_q_yoy", "ocf_to_profit"]
fund_frames = [neutralize(load_ranked(f, 1)) for f in FUND4]
score_fund4 = build_composite(fund_frames)
score_prod_fund = build_composite([sz, am] + fund_frames)
score_cfp = fund_frames[0].rename(columns={"r": "score"})

cases = [
    ("PROD(size+amihud)", score_prod),
    ("FUND4(中性化)", score_fund4),
    ("PROD+FUND4", score_prod_fund),
    ("CFP单腿(中性化)", score_cfp),
]

# ── 主评估（相位0）──
rows = []
for name, score in cases:
    t = time.time()
    ics = ic_series(score)
    ic, icir = float(ics.mean()), float(ics.mean() / ics.std())
    tm = tail_metrics(score)
    bt = standalone_bt(score)
    rows.append({"case": name, "ic_mean": round(ic, 4), "icir": round(icir, 3),
                 **tm, **bt})
    print(f"{name}: IC {ic:.4f} | tail_ann {tm['tail_spread_ann']:+.1%} "
          f"| size_rank {tm['size_rank_holdings']:.3f} "
          f"| annual {bt['annual']:+.1%} sharpe {bt['sharpe']} "
          f"({time.time()-t:.0f}s)", flush=True)

out = pd.DataFrame(rows)
out.to_csv(OUT_DIR / "ab_test.csv", index=False)
print(out.to_string(index=False), flush=True)

# ── 3 相位稳健性（闸门二）──
print("\n3 相位扫描…", flush=True)
phase_rows = []
for ph, offset in enumerate([0, 5, 10]):
    start = (pd.Timestamp(FULL_START) + pd.Timedelta(days=offset * 1.45)).strftime("%Y-%m-%d")
    for name, score in cases:
        bt = standalone_bt(score, start=start)
        phase_rows.append({"phase": ph, "start": start, "case": name, **bt})
        print(f"  相位{ph} {name}: annual {bt['annual']:+.1%} "
              f"sharpe {bt['sharpe']}", flush=True)

pdf = pd.DataFrame(phase_rows)
pdf.to_csv(OUT_DIR / "ab_phases.csv", index=False)

# 增量判定：每相位 PROD+FUND4 − PROD 与 FUND4 − PROD
print("\n=== 相位增量（闸门二判定）===")
for case_name in ["FUND4(中性化)", "PROD+FUND4"]:
    deltas = []
    for ph in range(3):
        a = pdf[(pdf["phase"] == ph) & (pdf["case"] == "PROD(size+amihud)")].iloc[0]
        b = pdf[(pdf["phase"] == ph) & (pdf["case"] == case_name)].iloc[0]
        deltas.append(b["annual"] - a["annual"])
        print(f"  {case_name} 相位{ph}: Δ年化 {deltas[-1]:+.1%}  "
              f"Δ夏普 {b['sharpe']-a['sharpe']:+.2f}")
    print(f"  → {case_name} 相位增量全正: {all(d > 0 for d in deltas)}")
