#!/usr/bin/env python3
"""P4b：二期 SUE 族直加测试 vs 生产（统一口径 + 3 相位）
=====================================================================
一批教训：中性化 FUND4 直加稀释 PROD（中盘化）。但 PROD_SI 先例
（sue_i 直加成功入候选队列）说明原始因子直加可行。

Case（全部四段等权 rank 合成，与 PROD 同构造）：
  PROD            = size+amihud_20（基准）
  PROD+sur_raw    = size+amihud_20+sur（原始直加）
  PROD+sue_gpoa_raw = size+amihud_20+sue_gpoa（原始直加）
  PROD+sur_neu    = size+amihud_20+中性化sur
  PROD+sue_gpoa_neu = size+amihud_20+中性化sue_gpoa
  PROD+SUE3       = size+amihud_20+sue_v2+sur+sue_gpoa（六段）

方向：sur/sue_gpoa/sue_v2 均为 +（高 SUE 好）。
输出: reports/fundamental_factor/ab_test_b2.csv / ab_phases_b2.csv
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
DB = str(ROOT / "data/quant.duckdb").replace("\\", "/")

FULL_START = "2022-07-01"
END = "2026-09-04"
REBAL = 20

con = duckdb.connect()

# 宇宙：官方 ashare_ex 口径（与生产/一批 P4 一致；主库锁已释放则可用，
# 被占用时回退 mirror instruments 全量并打印警告）
try:
    from quantlab.data.universe import get_universe
    UNI = set(get_universe("ashare_ex"))
    print(f"universe: ashare_ex {len(UNI)} 只", flush=True)
except Exception as e:
    _ins = con.execute("""
        SELECT code FROM read_parquet('C:/Users/53497/WorkBuddy/2026-09-02-23-42-28/quantlab/data/lake/clean/mirror/instruments.parquet')
    """).fetchdf()
    UNI = set(_ins["code"])
    print(f"⚠️ 主库被占用({str(e)[:60]}), 回退 mirror 全量宇宙 {len(UNI)} 只（含北交所）", flush=True)

# 价格：mirror 层 parquet 直读（绕开主库写锁）
px = con.execute("""
    SELECT CAST(date AS DATE) AS date, code, close * adj_factor AS c
    FROM read_parquet('C:/Users/53497/WorkBuddy/2026-09-02-23-42-28/quantlab/data/lake/clean/mirror/kline_daily.parquet')
""").fetchdf()
px["date"] = pd.to_datetime(px["date"])
PMAT = px.pivot(index="date", columns="code", values="c").sort_index()
print(f"pmat {PMAT.shape}", flush=True)

# —— size 原始秩（中性化用）——
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


def tail_metrics(score: pd.DataFrame) -> dict:
    """持仓市值暴露（价格全部来自 PMAT；本函数只报 size 秩）"""
    sc = score.sort_values(["date", "score"], ascending=[True, False])
    dates = sorted(sc["date"].unique())
    dates = [d for d in dates if d >= pd.Timestamp(FULL_START)]
    rb = dates[::REBAL]
    sr_idx = SRANK.set_index(["date", "code"])["srank"]
    sranks = []
    for d0 in rb:
        top = sc[sc["date"] == d0].head(100)
        if len(top) < 100 or d0 not in sr_idx.index.get_level_values(0):
            continue
        sranks.append(sr_idx.loc[d0].reindex(top["code"]).mean())
    return {"size_rank_holdings": round(float(np.nanmean(sranks)), 4)}


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


print("构造评分…", flush=True)
sz = load_ranked("size", -1)
am = load_ranked("amihud_20", 1)
sur_r = load_ranked("sur", 1)
sgp_r = load_ranked("sue_gpoa", 1)
sue2_r = load_ranked("sue_v2", 1)
si_r = load_ranked("sue_i", 1)          # 东财版融合 SUE（PROD_SI 成员）
om_r = load_ranked("overnight_mom_20", 1)
sur_n = neutralize(sur_r)
sgp_n = neutralize(sgp_r)

cases = [
    ("PROD(size+amihud)", build_composite([sz, am])),
    ("PROD_SI(4因子)", build_composite([sz, am, si_r, om_r])),
    ("PROD+sur_raw", build_composite([sz, am, sur_r])),
    ("PROD+sue_gpoa_raw", build_composite([sz, am, sgp_r])),
    ("PROD+sur_neu", build_composite([sz, am, sur_n])),
    ("PROD+SUE3(六段)", build_composite([sz, am, sue2_r, sur_r, sgp_r])),
    ("PROD_SI+sur(五段)", build_composite([sz, am, si_r, om_r, sur_r])),
]

rows = []
for name, score in cases:
    t = time.time()
    tm = tail_metrics(score)
    bt = standalone_bt(score)
    rows.append({"case": name, **tm, **bt})
    print(f"{name}: annual {bt['annual']:+.1%} sharpe {bt['sharpe']} "
          f"mdd {bt['mdd']:.1%} size_rank {tm['size_rank_holdings']:.3f} "
          f"({time.time()-t:.0f}s)", flush=True)

out = pd.DataFrame(rows)
out.to_csv(OUT_DIR / "ab_test_b2.csv", index=False)
print(out.to_string(index=False), flush=True)

print("\n3 相位扫描…", flush=True)
phase_rows = []
for ph, offset in enumerate([0, 5, 10]):
    start = (pd.Timestamp(FULL_START) + pd.Timedelta(days=offset * 1.45)).strftime("%Y-%m-%d")
    for name, score in cases:
        bt = standalone_bt(score, start=start)
        phase_rows.append({"phase": ph, "start": start, "case": name, **bt})
        print(f"  相位{ph} {name}: annual {bt['annual']:+.1%} sharpe {bt['sharpe']}", flush=True)

pdf = pd.DataFrame(phase_rows)
pdf.to_csv(OUT_DIR / "ab_phases_b2.csv", index=False)

print("\n=== 相位增量（闸门二判定）===")
for case_name in [c[0] for c in cases[1:]]:
    deltas = []
    for ph in range(3):
        a = pdf[(pdf["phase"] == ph) & (pdf["case"] == "PROD(size+amihud)")].iloc[0]
        b = pdf[(pdf["phase"] == ph) & (pdf["case"] == case_name)].iloc[0]
        deltas.append(b["annual"] - a["annual"])
    print(f"  {case_name}: Δ年化 {[f'{d:+.1%}' for d in deltas]} "
          f"全正={all(d > 0 for d in deltas)}")
