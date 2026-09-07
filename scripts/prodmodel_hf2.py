# -*- coding: utf-8 -*-
"""第六轮：增量合并测试 + 容量补测 + 持仓归因

1) EQ3_HFA（core+sue_i+hf_amihud_20）：两个已验证增量的合并，同窗回测
2) 各模型最新持仓 ADV 流动性画像（容量口径与第四轮一致）
3) 最新 top100 持仓两两重合度（hf_amihud 到底换了哪些票）
产出 reports/prodmodel/hf2.json
"""
import json
import sys
import time
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

import pandas as pd

from quantlab.model import build_composite
from quantlab.model.backtest import performance
from quantlab.optimize.backtest import run_optimized_backtest

OUT = "reports/prodmodel/hf2.json"
START, END = "2025-02-01", "2026-09-04"

MODELS = {
    "PROD": ["size", "amihud_20"],
    "EQ3": ["size", "amihud_20", "sue_i"],
    "HF_AMIH": ["size", "amihud_20", "hf_amihud_20"],
    "EQ3_HFA": ["size", "amihud_20", "sue_i", "hf_amihud_20"],
    "HFA_OM": ["size", "amihud_20", "hf_amihud_20", "overnight_mom_20"],
}

IC_SQL = """
WITH fwd AS (
    SELECT date, code, c_lead / c - 1 AS fwd
    FROM (
        SELECT date, code, close * adj_factor AS c,
               LEAD(close * adj_factor, 20) OVER (
                   PARTITION BY code ORDER BY date) AS c_lead
        FROM kline_daily)
    WHERE c_lead IS NOT NULL AND c > 0),
j AS (
    SELECT CAST(s.date AS DATE) AS date, s.score, w.fwd
    FROM score_df s JOIN fwd w ON CAST(s.date AS DATE) = w.date
         AND s.code = w.code),
rk AS (
    SELECT date, score, fwd,
           rank() OVER (PARTITION BY date ORDER BY score) AS rv,
           rank() OVER (PARTITION BY date ORDER BY fwd) AS rf,
           count(*) OVER (PARTITION BY date) AS n
    FROM j WHERE score IS NOT NULL)
SELECT date, corr(rv, rf) AS ic FROM rk WHERE n >= 300 GROUP BY date
"""


def score_ic(score: pd.DataFrame) -> dict:
    from quantlab.data.store import Store
    store = Store()
    sc = score.copy()
    sc["date"] = pd.to_datetime(sc["date"]).dt.date
    store.con.register("score_df", sc)
    df = store.q(IC_SQL)
    ic = df["ic"].dropna()
    df["date"] = pd.to_datetime(df["date"])
    df["year"] = df["date"].dt.year
    return {"ic_mean": round(float(ic.mean()), 4),
            "icir": round(float(ic.mean() / ic.std()), 3),
            "yearly": {int(y): round(float(v), 4)
                       for y, v in df.groupby("year")["ic"].mean().items()}}


def latest_holdings(score: pd.DataFrame, n: int = 100) -> set:
    d = score["date"].max()
    top = (score[score["date"] == d]
           .nlargest(n, "score")["code"])
    return set(top)


def adv_profile(codes: set) -> dict:
    from quantlab.data.store import Store
    store = Store()
    cl = ",".join(f"'{c}'" for c in codes)
    df = store.q(f"""
        SELECT code, avg(amount) AS adv
        FROM (SELECT code, amount FROM kline_daily WHERE code IN ({cl})
              ORDER BY date DESC LIMIT 20 * {len(codes)})
        GROUP BY code""")
    adv = df["adv"].dropna() / 1e8
    return {"n": int(len(adv)),
            "adv_med_yi": round(float(adv.median()), 3),
            "adv_p10_yi": round(float(adv.quantile(0.10)), 3)}


def monthly_diff(cv_a: pd.DataFrame, cv_b: pd.DataFrame) -> dict:
    a = cv_a.copy()
    b = cv_b.copy()
    for cv in (a, b):
        cv["date"] = pd.to_datetime(cv["date"])
        cv["ret"] = cv["nav"].pct_change()
        cv["ym"] = cv["date"].dt.to_period("M")
    diff = (a.groupby("ym")["ret"].sum()
            - b.groupby("ym")["ret"].sum()).dropna()
    return {"n": int(len(diff)), "win": round(float((diff > 0).mean()), 3),
            "mean_pp": round(float(diff.mean()) * 100, 2)}


def main():
    results, curves, holds = {}, {}, {}
    for name, factors in MODELS.items():
        t0 = time.time()
        score = build_composite(factors, universe="ashare_ex", start=START)
        bt = run_optimized_backtest(score.copy(), n_stocks=100, rebalance=20,
                                    method="inverse_vol",
                                    start=START, end=END)
        m = bt["metrics"]
        perf = performance(bt["curve"].set_index("date")["nav"])
        ic = score_ic(score)
        c = bt["curve"].copy()
        c["year"] = pd.to_datetime(c["date"]).dt.year
        results[name] = {
            "factors": factors,
            "annual": round(float(perf["annual_return"]), 4),
            "sharpe": round(float(perf["sharpe"]), 2),
            "mdd": round(float(perf["max_drawdown"]), 4),
            "turnover": round(float(bt["turnover_avg"]), 4),
            "ic": ic,
            "yearly": {int(y): round(float((1 + g["ret"]).prod() - 1), 4)
                       for y, g in c.groupby("year")},
        }
        curves[name] = bt["curve"][["date", "nav"]]
        holds[name] = latest_holdings(score)
        print(f"{name}: ann {results[name]['annual']*100:.1f}% "
              f"sharpe {results[name]['sharpe']} "
              f"mdd {results[name]['mdd']*100:.1f}% "
              f"to {results[name]['turnover']*100:.1f}% "
              f"ICIR {ic['icir']} ({time.time()-t0:.0f}s)", flush=True)

    # 月度配对：vs PROD 与 vs HF_AMIH
    md = {}
    for name in curves:
        if name != "PROD":
            md[f"{name}_vs_PROD"] = monthly_diff(curves[name], curves["PROD"])
        if name not in ("PROD", "HF_AMIH"):
            md[f"{name}_vs_HF_AMIH"] = monthly_diff(curves[name],
                                                    curves["HF_AMIH"])
    results["_monthly"] = md

    # 容量画像 + 持仓重合度
    cap = {m: adv_profile(h) for m, h in holds.items()}
    ovl = {}
    names = list(holds)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            ovl[f"{a}|{b}"] = len(holds[a] & holds[b])
    results["_capacity"] = cap
    results["_overlap_top100"] = ovl
    print("capacity:", json.dumps(cap), flush=True)
    print("overlap:", json.dumps(ovl), flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1, default=str)
    print("saved", OUT, flush=True)


if __name__ == "__main__":
    main()
