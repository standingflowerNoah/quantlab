# -*- coding: utf-8 -*-
"""hf 批次 R2 交易成本：A 层 9 因子月频调仓盈亏平衡成本扫描
=================================================================
评价框架 R2 闸门（research/factor_eval_dimensions_20260912.md §3），
全部无锁（metric_store L0 + 因子湖）：

  1) 年化毛超额（多空，月频口径）
       G_bp = |mean(ls_ret20)| × 12 × 10000
     ls_ret20 为 L0 每日信号 20 交易日累计五分位多空收益，
     月频调仓持有 20 日的月均多空收益 ≈ mean(ls_ret20)。
     多空组合可整体反向（IC 负的因子反向持有），故取绝对值。
  2) 月频单边换手 u = 1 − 相邻月末持仓重叠率
     组合口径 top100（与 PROD 可比）与 q20% 两版；
     多头边按 L0 rank_ic20 符号选（IC<0 → 取低值组）。
  3) 盈亏平衡单边成本 be_bp = G_bp / (24 × u)
     年 12 次调仓 × 多空双边各换手 u（对称近似）。
     对比实测成本线：≥20bp 🟢 / 15~20bp 🟡 / <15bp 🔴。

用法：python scripts/hf_r2_cost.py [--factors a,b,...]
产出：reports/_tmp/hf_r2_cost_summary.csv
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
METRIC_GLOB = str(ROOT / "data/lake/factor_metric_daily/ashare_ex/part-*.parquet")
FACTOR_DIR = ROOT / "data/lake/factor"
OUT_CSV = ROOT / "reports/_tmp/hf_r2_cost_summary.csv"

# A 层 9 因子（FDR+R1 双过，R3 已登记 economic_rationale）
A9 = ["hf_amihud_20", "hf_rsk_20", "hf_rku_20", "hf_dsem_20",
      "hf_vopen_20", "hf_vampp_20", "hf_rfirst30_20", "hf_corr_rv_20",
      "hf_topvr_20"]

CON = duckdb.connect()
CON.execute("SET memory_limit='2GB'")
CON.execute("SET threads=4")
CON.execute("SET preserve_insertion_order=false")


# ---------------------------------------------------------------- 数据层
def load_l0(factors: list[str]) -> pd.DataFrame:
    lst = ",".join(f"'{f}'" for f in factors)
    df = CON.execute(f"""
        SELECT factor, CAST(date AS DATE) AS date, ls_ret20, rank_ic20
        FROM read_parquet('{METRIC_GLOB}')
        WHERE factor IN ({lst}) ORDER BY factor, date
    """).fetchdf()
    return df


def month_end_dates(dates: pd.Series) -> list:
    s = pd.Series(pd.to_datetime(dates)).drop_duplicates().sort_values()
    return list(s.groupby(s.dt.to_period("M")).max())


def load_me_values(factors: list[str], me_list: list) -> pd.DataFrame:
    """因子湖月末截面（9 因子一次取齐）。"""
    dl = ",".join(f"'{pd.Timestamp(d).date()}'" for d in me_list)
    frames = []
    for fac in factors:
        f = CON.execute(f"""
            SELECT CAST(date AS DATE) AS date, code, value
            FROM read_parquet('{(FACTOR_DIR / fac).as_posix()}/*.parquet')
            WHERE CAST(date AS DATE) IN ({dl})
        """).fetchdf()
        f["factor"] = fac
        frames.append(f)
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------- 换手层
def portfolio_chain(me: pd.DataFrame, long_low: bool,
                    topn: int | None, frac: float | None) -> list[set]:
    """月末持仓链。long_low=True 取低值组（IC<0 因子的多头边）。"""
    m = me.dropna(subset=["value"]).sort_values(
        "value", ascending=long_low, kind="mergesort")
    holds = []
    for d, g in m.groupby("date"):
        if topn is not None:
            sel = g.head(topn)
        else:
            k = max(1, int(round(len(g) * frac)))
            sel = g.head(k)
        holds.append((d, set(sel["code"])))
    holds.sort(key=lambda x: x[0])
    return [h for _, h in holds]


def chain_turnover(holds: list[set]) -> float:
    """单边月换手 = 1 − 平均重叠率。"""
    if len(holds) < 2:
        return np.nan
    ovs = [len(a & b) / max(1, len(b)) for a, b in zip(holds, holds[1:])]
    return float(1.0 - np.mean(ovs))


# ---------------------------------------------------------------- 主流程
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--factors", help="逗号分隔子集（默认 A 层 9 因子）")
    a = ap.parse_args()
    factors = ([s.strip() for s in a.factors.split(",")] if a.factors else A9)

    t0 = time.time()
    print("加载 L0（ls_ret20 / rank_ic20）...", flush=True)
    l0 = load_l0(factors)
    me_list = month_end_dates(l0["date"])
    print(f"  L0 {len(l0):,} 行 / 月末 {len(me_list)} 个 "
          f"({me_list[0]} ~ {me_list[-1]})", flush=True)

    print("加载因子湖月末截面...", flush=True)
    me = load_me_values(factors, me_list)
    print(f"  月末截面 {len(me):,} 行 ({time.time()-t0:.0f}s)", flush=True)

    rows = []
    for fac in factors:
        sub = l0[l0["factor"] == fac]
        ls_mean = float(sub["ls_ret20"].mean())
        ic_mean = float(sub["rank_ic20"].mean())
        g_bp = abs(ls_mean) * 12 * 10000          # 年化毛超额（多空，bp）
        long_low = ic_mean < 0                     # 多头边方向
        m = me[me["factor"] == fac]
        u_top = chain_turnover(portfolio_chain(m, long_low, 100, None))
        u_q20 = chain_turnover(portfolio_chain(m, long_low, None, 0.20))
        turns_ls = 24 * u_top                      # 多空双边对称近似
        be_bp = g_bp / turns_ls if turns_ls and np.isfinite(turns_ls) else np.nan
        verdict = ("🟢" if be_bp >= 20 else "🟡" if be_bp >= 15 else "🔴")
        rows.append({
            "factor": fac,
            "ic20_mean": round(ic_mean, 4),
            "ls20_mean": round(ls_mean, 4),
            "G_ann_ls_bp": round(g_bp, 1),
            "u_top100": round(u_top, 3),
            "u_q20": round(u_q20, 3),
            "turns_ann_ls": round(turns_ls, 1),
            "be_bp": round(be_bp, 1),
            "R2_verdict": verdict,
        })
        print(f"{fac}: G={g_bp:.0f}bp u={u_top:.2f} be={be_bp:.1f}bp {verdict}",
              flush=True)

    out = pd.DataFrame(rows)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\n汇总 → {OUT_CSV}")
    print(out.to_string(index=False))


if __name__ == "__main__":
    main()
