# -*- coding: utf-8 -*-
"""hf 系因子加入生产组合的增量检验（用户指令：|IC| 选样、负向翻转）

统一窗口 2025-02-01 ~ 2026-09-04（hf 因子 2025-01-22 起有值，留暖机）。
所有模型同窗回测（top100 / 20日 / inverse_vol / 全成本），公平对比。
产出 reports/prodmodel/hf_add.json
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

OUT = "reports/prodmodel/hf_add.json"
HF_START = "2025-02-01"
END = "2026-09-04"

CORE = ["size", "amihud_20"]
# 按 |ICIR| 取最强的 hf 因子（排除 hf_amihud_20：ρ=0.86 vs amihud_20）
HF_TOP5 = ["hf_rsk_20", "hf_dsem_20", "hf_topvr_20",
           "hf_corr_rv_20", "hf_amtrange_20"]

MODELS = {
    "PROD": CORE,
    "EQ3": CORE + ["sue_i"],
    "HF_AMIH": CORE + ["hf_amihud_20"],          # 冗余对照：分钟精化版是否真有增量
    "HF3": CORE + HF_TOP5[:3],
    "HF5": CORE + HF_TOP5,
    "EQ3_HF3": CORE + ["sue_i"] + HF_TOP5[:3],
    "HFWF": CORE + ["hf_wf_composite"],
    "EQ3_HFWF": CORE + ["sue_i", "hf_wf_composite"],
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
    yearly = df.groupby("year")["ic"].mean()
    return {"ic_mean": round(float(ic.mean()), 4),
            "icir": round(float(ic.mean() / ic.std()), 3),
            "ic_win": round(float((ic > 0).mean()), 3),
            "yearly": {int(y): round(float(v), 4)
                       for y, v in yearly.items()}}


def run_one(name: str, factors: list) -> dict:
    t0 = time.time()
    score = build_composite(factors, universe="ashare_ex", start=HF_START)
    bt = run_optimized_backtest(score.copy(), n_stocks=100, rebalance=20,
                                method="inverse_vol",
                                start=HF_START, end=END)
    m = bt["metrics"]
    nav = bt["curve"].set_index("date")["nav"]
    perf = performance(nav)
    ic = score_ic(score)
    # 分年收益
    c = bt["curve"].copy()
    c["year"] = pd.to_datetime(c["date"]).dt.year
    yearly = {int(y): round(float((1 + g["ret"]).prod() - 1), 4)
              for y, g in c.groupby("year")}
    out = {"factors": factors,
           "annual": round(float(perf["annual_return"]), 4),
           "sharpe": round(float(perf["sharpe"]), 2),
           "mdd": round(float(perf["max_drawdown"]), 4),
           "calmar": round(float(perf.get("calmar", perf["annual_return"]
                                          / abs(perf["max_drawdown"]))), 2),
           "turnover": round(float(bt["turnover_avg"]), 4),
           "yearly": yearly,
           "ic": ic,
           "curve": bt["curve"][["date", "nav", "nav_bm"]].copy()}
    print(f"{name}: ann {out['annual']*100:.1f}% sharpe {out['sharpe']} "
          f"mdd {out['mdd']*100:.1f}% to {out['turnover']*100:.1f}% "
          f"IC {ic['ic_mean']} ICIR {ic['icir']} "
          f"({time.time()-t0:.0f}s)", flush=True)
    return out


def main():
    results, curves = {}, {}
    for name, factors in MODELS.items():
        try:
            r = run_one(name, factors)
            results[name] = {k: v for k, v in r.items() if k != "curve"}
            curves[name] = r["curve"]
        except Exception as e:
            print(f"{name}: FAILED {e}", flush=True)

    # 月度配对差 vs PROD
    if "PROD" in curves:
        base = curves["PROD"].copy()
        base["date"] = pd.to_datetime(base["date"])
        base["ret"] = base["nav"].pct_change()
        base["ym"] = base["date"].dt.to_period("M")
        bm = base.groupby("ym")["ret"].sum()
        monthly = {}
        for name, cv in curves.items():
            if name == "PROD":
                continue
            cv = cv.copy()
            cv["date"] = pd.to_datetime(cv["date"])
            cv["ret"] = cv["nav"].pct_change()
            cv["ym"] = cv["date"].dt.to_period("M")
            mm = cv.groupby("ym")["ret"].sum()
            diff = (mm - bm).dropna()
            monthly[name] = {"n": int(len(diff)),
                             "win": round(float((diff > 0).mean()), 3),
                             "mean_pp": round(float(diff.mean()) * 100, 2)}
        results["_monthly_vs_prod"] = monthly

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1, default=str)
    # 曲线留 pickle 供报告用
    pd.to_pickle(curves, "reports/prodmodel/hf_add_curves.pkl")
    print("saved", OUT, flush=True)


if __name__ == "__main__":
    main()
