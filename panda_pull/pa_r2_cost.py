# -*- coding: utf-8 -*-
"""PandaAI 复现因子批次 R2 交易成本：月频调仓盈亏平衡成本（无锁 mirror 路径）
=============================================================================
评价框架 R2 闸门（research/factor_eval_dimensions_20260912.md §3）。
pa_ 因子无 L0 metric 行（run_eval 现场重算未落 factor_metric_daily），
故自算与 hf_r2_cost.py 等价的量：

  1) 年化毛超额（五分位多空，真月频调仓）
       每个月末按因子值分 5 组（IC>0 → 高值组为多头），
       多空 = Q5 均值收益 − Q1 均值收益（月末→下月末，close×adj_factor）
       G_bp = |mean(ls_monthly)| × 12 × 10000
  2) 月频单边换手 u = 1 − 相邻月末持仓重叠率（top100 与 q20% 两版）
  3) 盈亏平衡单边成本 be_bp = G_bp / (24 × u_top100)
     ≥20bp 🟢 / 15~20bp 🟡 / <15bp 🔴

局限（读表须知）：未剔 ST/封板（与 hf_r2_cost 同口径近似）；
月末收盘成交假设（妖股月偏差，组合层 A/B 时必须复检）。

用法：quantlab venv python panda_pull/pa_r2_cost.py
产出：reports/_tmp/pa_r2_cost_summary.csv
"""
from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

FACTOR_DIR = "data/lake/factor"
MIRROR = "data/lake/clean/mirror/kline_daily.parquet"
OUT_CSV = "reports/_tmp/pa_r2_cost_summary.csv"

PA = ["pa_a101_040", "pa_a101_044", "pa_a101_088", "pa_5d_min_low_ratio"]

CON = duckdb.connect()
CON.execute("SET memory_limit='2GB'")
CON.execute("SET threads=4")


def month_end_dates(factor: str) -> list:
    d = CON.execute(f"""
        SELECT DISTINCT CAST(date AS DATE) AS date
        FROM read_parquet('{FACTOR_DIR}/{factor}/*.parquet')
        WHERE date >= DATE '2023-01-01'
    """).fetchdf()["date"]
    s = pd.Series(pd.to_datetime(d)).sort_values()
    return list(s.groupby(s.dt.to_period("M")).max())


def load_me(factor: str, me_list: list) -> pd.DataFrame:
    dl = ",".join(f"'{pd.Timestamp(d).date()}'" for d in me_list)
    return CON.execute(f"""
        SELECT CAST(date AS DATE) AS date, code, value
        FROM read_parquet('{FACTOR_DIR}/{factor}/*.parquet')
        WHERE CAST(date AS DATE) IN ({dl})
    """).fetchdf()


def load_mirror_close_adj(me_list: list) -> pd.DataFrame:
    """月末 close×adj_factor 长表 → pivot。末月无下月末价格的自动配对失败被丢弃。"""
    dl = ",".join(f"'{pd.Timestamp(d).date()}'" for d in me_list)
    df = CON.execute(f"""
        SELECT CAST(date AS DATE) AS date, code,
               close * adj_factor AS close_adj
        FROM read_parquet('{MIRROR}')
        WHERE CAST(date AS DATE) IN ({dl}) AND close > 0 AND adj_factor > 0
    """).fetchdf()
    return df.pivot(index="code", columns="date", values="close_adj")


def chain_turnover(holds: list[set]) -> float:
    if len(holds) < 2:
        return np.nan
    ovs = [len(a & b) / max(1, len(b)) for a, b in zip(holds, holds[1:])]
    return float(1.0 - np.mean(ovs))


def main() -> None:
    me_list = month_end_dates(PA[0])
    print(f"月末 {len(me_list)} 个: {me_list[0].date()} ~ {me_list[-1].date()}",
          flush=True)
    prices = load_mirror_close_adj(me_list)
    print(f"mirror 月末价 {prices.shape[0]:,} 只", flush=True)

    rows = []
    for fac in PA:
        me = load_me(fac, me_list)
        # 按 (date) 分组 → 五分位多空 + top100/q20 持仓链
        ls_rets = []
        holds100, holds20 = [], []
        for d, g in me.groupby("date"):
            g = g.dropna(subset=["value"])
            if len(g) < 500:
                continue
            g = g.sort_values("value", ascending=False, kind="mergesort")
            k = max(1, len(g) // 5)
            top_codes = set(g.head(k)["code"])
            bot_codes = set(g.tail(k)["code"])
            holds100.append(set(g.head(100)["code"]))
            holds20.append(set(g.head(max(1, int(round(len(g) * 0.20))))["code"]))
            # 下月末收益
            later = [x for x in me_list if x > d]
            if not later:
                continue
            d2 = later[0]
            if d2 not in prices.columns:
                continue
            p0, p1 = prices[d], prices[d2]
            ret = (p1 / p0 - 1).dropna()
            ret = ret[ret.between(-0.85, 3.0)]  # 极端脏值防御
            if len(ret) < 500:
                continue
            q5 = ret.reindex(top_codes).dropna().mean()
            q1 = ret.reindex(bot_codes).dropna().mean()
            if np.isfinite(q5) and np.isfinite(q1):
                ls_rets.append(q5 - q1)
        ls_mean = float(np.mean(ls_rets))
        g_bp = abs(ls_mean) * 12 * 10000
        u_top = chain_turnover(holds100)
        u_q20 = chain_turnover(holds20)
        be_bp = g_bp / (24 * u_top) if u_top and np.isfinite(u_top) else np.nan
        verdict = ("🟢" if be_bp >= 20 else "🟡" if be_bp >= 15 else "🔴")
        rows.append({
            "factor": fac,
            "n_months": len(ls_rets),
            "ls_monthly_mean": round(ls_mean, 4),
            "G_ann_ls_bp": round(g_bp, 1),
            "u_top100": round(u_top, 3),
            "u_q20": round(u_q20, 3),
            "turns_ann_ls": round(24 * u_top, 1),
            "be_bp": round(be_bp, 1),
            "R2_verdict": verdict,
        })
        print(f"{fac}: G={g_bp:.0f}bp u_top={u_top:.2f} u_q20={u_q20:.2f} "
              f"be={be_bp:.1f}bp {verdict}", flush=True)

    out = pd.DataFrame(rows)
    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(f"\n汇总 → {OUT_CSV}")
    print(out.to_string(index=False))


if __name__ == "__main__":
    main()
