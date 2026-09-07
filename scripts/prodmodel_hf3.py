# -*- coding: utf-8 -*-
"""第七轮：EQ3_HFA 深化——子域检验 / 权重法 / hf_amihud 独特信息归因

1) 子域检验：剔除每日市值底部 30% 后重测 PROD/EQ3/EQ3_HFA——回答"是不是纯微盘 beta"
2) 权重法：EQ3_HFA 的 ic_weighted 变体 vs 等权
3) 归因：hf_amihud_20 相对 amihud_20 的秩残差的 IC20（独特信息量化）
产出 reports/prodmodel/hf3.json
"""
import json
import sys
import time
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

import pandas as pd

from quantlab.model import build_composite, FACTOR_DIRECTION
from quantlab.model.backtest import performance
from quantlab.optimize.backtest import run_optimized_backtest

OUT = "reports/prodmodel/hf3.json"
START, END = "2025-02-01", "2026-09-04"

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
            "n_days": int(len(ic)),
            "yearly": {int(y): round(float(v), 4)
                       for y, v in df.groupby("year")["ic"].mean().items()}}


def bt_pack(score: pd.DataFrame, tag: str) -> dict:
    t0 = time.time()
    r = run_optimized_backtest(score.copy(), n_stocks=100, rebalance=20,
                               method="inverse_vol",
                               start=START, end=END)
    m = r["metrics"]
    perf = performance(r["curve"].set_index("date")["nav"])
    c = r["curve"].copy()
    c["year"] = pd.to_datetime(c["date"]).dt.year
    out = {"annual": round(float(perf["annual_return"]), 4),
           "sharpe": round(float(perf["sharpe"]), 2),
           "mdd": round(float(perf["max_drawdown"]), 4),
           "turnover": round(float(r["turnover_avg"]), 4),
           "yearly": {int(y): round(float((1 + g["ret"]).prod() - 1), 4)
                      for y, g in c.groupby("year")},
           "ic": score_ic(score)}
    print(f"{tag}: ann {out['annual']*100:.1f}% sharpe {out['sharpe']} "
          f"mdd {out['mdd']*100:.1f}% ({time.time()-t0:.0f}s)", flush=True)
    return out


def load_mcap() -> pd.DataFrame:
    from quantlab.data.store import Store
    m = Store().read_factor("total_mcap")
    m["date"] = pd.to_datetime(m["date"])
    return m[["date", "code", "value"]].rename(columns={"value": "mcap"})


def main():
    results = {}

    # ── 1) 子域检验：剔市值底部 30% ──
    mcap = load_mcap()
    models = {
        "PROD": ["size", "amihud_20"],
        "EQ3": ["size", "amihud_20", "sue_i"],
        "EQ3_HFA": ["size", "amihud_20", "sue_i", "hf_amihud_20"],
    }
    for name, factors in models.items():
        score = build_composite(factors, universe="ashare_ex", start=START)
        sc = score.merge(mcap, on=["date", "code"], how="left")
        # 每日截面 30 分位，仅保留市值高于分位的股票
        q = sc.groupby("date")["mcap"].transform(
            lambda x: x.quantile(0.30))
        sc = sc[sc["mcap"] > q].drop(columns="mcap")
        results[f"SUB_{name}"] = bt_pack(sc, f"SUB_{name}")
        results[f"SUB_{name}"]["kept_ratio"] = round(
            float(len(sc)) / len(score), 3)

    # ── 2) 权重法：ic_weighted 变体 ──
    score_w = build_composite(models["EQ3_HFA"], universe="ashare_ex",
                              start=START, method="ic_weighted")
    results["EQ3_HFA_ICW"] = bt_pack(score_w, "EQ3_HFA_ICW")

    # ── 3) hf_amihud 独特信息：秩残差 IC ──
    from quantlab.data.store import Store
    store = Store()
    resid_frames = []
    for f in ("amihud_20", "hf_amihud_20"):
        fv = store.read_factor(f)
        fv["date"] = pd.to_datetime(fv["date"])
        d = FACTOR_DIRECTION.get(f, 1)
        fv["r"] = fv.groupby("date")["value"].rank(pct=True) * d
        resid_frames.append(fv[["date", "code", "r"]].set_index(
            ["date", "code"]))
    resid = (resid_frames[1]["r"] - resid_frames[0]["r"]).dropna()
    resid = resid[resid.abs() > 1e-9].reset_index()
    resid.columns = ["date", "code", "score"]
    resid = resid[resid["date"] >= pd.Timestamp(START)]
    results["RESID_HF_AMIH"] = score_ic(resid)
    print("RESID_HF_AMIH:", results["RESID_HF_AMIH"], flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1, default=str)
    print("saved", OUT, flush=True)


if __name__ == "__main__":
    main()
