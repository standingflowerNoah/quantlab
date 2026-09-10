#!/usr/bin/env python3
"""诊断：高 IC 因子为何进不了生产模型 / 候选模型为何跑不过简单模型
=====================================================================
目标函数错位假说：Rank IC 是全截面（~4700 只）统计量，而 long-only 组合
只吃尾部 100 只（2%）。本脚本对同一批高 IC 因子同时测量：
  A. 全截面 Rank IC20（与 prodmodel_eval 完全同口径）
  B. 尾部选股力：每个调仓日 top100 的 fwd20 均值 − 宇宙均值（tail spread）
  C. 持仓市值暴露：top100 的 size 秩均值（低=微盘）
  D. 单腿组合回测（与统一回测口径完全一致：top100/20日/inverse_vol/全成本）

若 B/C 与 A 脱节（IC 高但尾部 spread 低、市值暴露不极端），则"IC 好≠组合好"
是真实现象而非管线 bug；若 B 与 A 同高但 D 差，则需怀疑回测引擎。

惯例：in-memory duckdb + ATTACH READ_ONLY，不碰主库写锁。
输出: reports/prodmodel/diag_ic_vs_portfolio.json + 控制台表格
"""
from __future__ import annotations

import json
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
OUT_DIR = ROOT / "reports/prodmodel"
DB = str(ROOT / "data/quant.duckdb").replace("\\", "/")

FULL_START = "2022-07-01"
END = "2026-09-04"

con = duckdb.connect()
con.execute(f"ATTACH '{DB}' AS q (READ_ONLY)")

# —— 前瞻 20 日收益（与 prodmodel_eval 同口径）——
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

# —— 宇宙 ——
from quantlab.data.universe import get_universe
UNI = set(get_universe("ashare_ex"))

# —— 价格矩阵（共享，供回测）——
t0 = time.time()
px = con.execute("""
    SELECT CAST(date AS DATE) AS date, code, close * adj_factor AS c
    FROM q.kline_daily
""").fetchdf()
px["date"] = pd.to_datetime(px["date"])
PMAT = px.pivot(index="date", columns="code", values="c").sort_index()
con.execute("DETACH q")   # 回测引擎内部要开主库，先释放句柄（fwd20 已物化）
print(f"pmat {PMAT.shape} ({time.time()-t0:.0f}s)")

# —— size 原始秩（不含方向；低=微盘）——
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
    """因子秩标准化到 [0,1]（含方向），限制宇宙——与 build_composite 等价"""
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


def build_composite(names: list[str], directions: dict) -> pd.DataFrame:
    frames = [load_ranked(n, directions.get(n, 1)) for n in names]
    allf = pd.concat(frames, ignore_index=True)
    return (allf.groupby(["date", "code"])["r"].mean()
            .reset_index().rename(columns={"r": "score"}))


def full_ic(score: pd.DataFrame) -> tuple[float, float]:
    """全截面 Rank IC20 均值 / ICIR（注册临时表 + SQL，与 eval 脚本同口径）"""
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
    return float(ic.mean()), float(ic.mean() / ic.std())


def tail_metrics(score: pd.DataFrame) -> dict:
    """尾部选股力：每 20 交易日取 top100，测 fwd20 spread 与 size 秩暴露"""
    fwd = con.execute("""
        SELECT CAST(date AS DATE) AS date, code, fwd FROM fwd20
    """).fetchdf()
    fwd["date"] = pd.to_datetime(fwd["date"])
    fwd = fwd[fwd["code"].isin(UNI)]
    fwd_idx = fwd.set_index(["date", "code"])["fwd"]
    uni_mean = fwd_idx.groupby(level=0).mean()
    sr_idx = SRANK.set_index(["date", "code"])["srank"]

    sc = score.sort_values(["date", "score"], ascending=[True, False])
    dates = sorted(sc["date"].unique())
    dates = [d for d in dates if d >= pd.Timestamp(FULL_START)]
    rb = dates[::20]
    spreads, sranks = [], []
    for d0 in rb:
        top = sc[sc["date"] == d0].head(100)
        if len(top) < 100 or d0 not in uni_mean.index:
            continue
        f = fwd_idx.loc[d0]                     # Series indexed by code
        vals = f.reindex(top["code"]).dropna()
        if len(vals) < 80:
            continue
        spreads.append(vals.mean() - uni_mean.loc[d0])
        sranks.append(sr_idx.loc[d0].reindex(top["code"]).mean())
    sp = pd.Series(spreads, index=rb[:len(spreads)]).dropna()
    return {
        "tail_spread_20d": round(float(sp.mean()), 5),        # 每 20 日超额
        "tail_spread_ann": round(float(sp.mean() * 252 / 20), 4),  # 年化近似
        "tail_win": round(float((sp > 0).mean()), 3),
        "size_rank_holdings": round(float(np.nanmean(sranks)), 4),
        "n_periods": int(len(sp)),
    }


def standalone_bt(score: pd.DataFrame) -> dict:
    from quantlab.optimize.backtest import run_optimized_backtest
    r = run_optimized_backtest(score.copy(), n_stocks=100, rebalance=20,
                               method="inverse_vol", start=FULL_START,
                               end=END, pmat=PMAT)
    m = r["metrics"]
    return {"annual": round(m["annual_return"], 4),
            "sharpe": round(m["sharpe"], 3),
            "mdd": round(m["max_drawdown"], 4),
            "turnover": r["turnover_avg"]}


IC20 = pd.read_csv(OUT_DIR / "factor_ic20.csv").set_index("factor")

DIR = {"size": -1, "amihud_20": 1, "dragon_net_20": -1, "amount_std_20": -1,
       "lockup_pressure_60": -1, "max_return_20": -1, "momentum_60": -1,
       "skewness_20": -1, "momentum_20": -1, "ev_high_vol_20": -1,
       "overnight_mom_20": 1, "alpha013": 1, "alpha126": 1, "alpha055": 1}

SATS_EX = ["dragon_net_20", "amount_std_20", "lockup_pressure_60",
           "max_return_20", "momentum_60", "skewness_20", "momentum_20",
           "ev_high_vol_20"]
STANDALONE = ["dragon_net_20", "amount_std_20", "alpha013", "alpha126",
              "alpha055", "overnight_mom_20", "max_return_20",
              "momentum_60", "lockup_pressure_60"]

rows = []

# —— PROD 与 CORE_EX 复合评分 ——
score_prod = build_composite(["size", "amihud_20"], DIR)
score_ex = build_composite(["size", "amihud_20"] + SATS_EX, DIR)

cases = []
for f in STANDALONE:
    cases.append((f, load_ranked(f, DIR.get(f, 1))
                  .rename(columns={"r": "score"})))
cases.append(("PROD(size+amihud)", score_prod))
cases.append(("CORE_EX(核心+8卫星)", score_ex))

for name, score in cases:
    t = time.time()
    ic, icir = full_ic(score)
    tm = tail_metrics(score)
    bt = standalone_bt(score)
    rows.append({"case": name, "ic_mean": round(ic, 4), "icir": round(icir, 3),
                 **tm, **bt})
    print(f"{name}: IC {ic:.4f} | tail_spread_ann {tm['tail_spread_ann']:+.1%} "
          f"| size_rank {tm['size_rank_holdings']:.3f} "
          f"| annual {bt['annual']:+.1%} sharpe {bt['sharpe']} "
          f"({time.time()-t:.0f}s)")

out = pd.DataFrame(rows)
out.to_csv(OUT_DIR / "diag_ic_vs_portfolio.csv", index=False)
print()
print(out.to_string(index=False))
print("\nsaved -> reports/prodmodel/diag_ic_vs_portfolio.csv")
