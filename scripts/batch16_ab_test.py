#!/usr/bin/env python3
"""batch16 · GRU 组合级 A/B：gru_seq_score vs PROD vs LGBM 系（2026-09-10）
======================================================================
升级版闸门三口径（batch12 框架复用）：
  ①组内相对（同 ML 族内排名是否变化）
  ②focus 窗口 4 相位逐相位超额 Δ
  ③full 窗口（不新增调参自由度）
模型：PROD_SI / LGBM_280 / LGBM_64（归因对照）/ GRU_64 / ML_MIX（等权 blend）
END 对齐各 score 覆盖终点 2026-08-06。
输出：reports/batch16_ab_20260910.json
"""
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import json

import pandas as pd

from quantlab.model import build_composite, run_multiphase_backtest

N, REB, PHASES = 100, 20, 4
WINDOWS = {"focus": "2025-05-01", "full": "2024-01-01"}
END = "2026-08-06"


def yearly(curve):
    c = curve.copy()
    c["year"] = pd.to_datetime(c["date"]).dt.year
    return {int(y): round(float((1 + g["ret"]).prod() - 1), 4)
            for y, g in c.groupby("year")}


def pack(res):
    m, e = res["metrics"], res["excess"]
    return {"annual": m["annual_return"], "sharpe": m["sharpe"],
            "mdd": m["max_drawdown"], "excess": e["excess_annual"],
            "ir": e["information_ratio"], "yearly": yearly(res["curve"]),
            "phase_detail": res["phase_detail"]}


def load_score(name):
    fv = pd.read_parquet(f"data/lake/factor/{name}/part-b00001.parquet")
    fv["date"] = pd.to_datetime(fv["date"])
    fv["score"] = fv.groupby("date")["score"].rank(pct=True)
    return fv[["date", "code", "score"]]


def main():
    t0 = time.time()
    scores = {
        "PROD_SI": build_composite(["size", "amihud_20", "sue_i",
                                    "overnight_mom_20"],
                                   universe="ashare_ex"),
        "LGBM_280": load_score("lgbm_all_score"),
        "LGBM_64": load_score("lgbm_top64_score"),
        "GRU_64": load_score("gru_seq_score"),
    }
    # ML_MIX：三个 ML score 等权 rank 混合
    lgb64, gru64 = scores["LGBM_64"], scores["GRU_64"]
    mix = lgb64.merge(gru64, on=["date", "code"], suffixes=("_l", "_g"))
    mix["score"] = (mix["score_l"] + mix["score_g"]) / 2
    scores["ML_MIX"] = mix[["date", "code", "score"]]
    for k, v in scores.items():
        print(f"  {k}: {len(v)} 行 {v['date'].nunique()} 天", flush=True)

    out = {"config": {"n": N, "reb": REB, "end": END, "windows": WINDOWS}}
    for wname, wstart in WINDOWS.items():
        out[wname] = {}
        for tag, sc in scores.items():
            r = run_multiphase_backtest(sc.copy(), n_stocks=N, rebalance=REB,
                                        phases=PHASES, start=wstart, end=END)
            out[wname][tag] = pack(r)
            m, e = r["metrics"], r["excess"]
            print(f"  {wname}/{tag:9s} 年化{m['annual_return']:+7.1%} "
                  f"夏普{m['sharpe']:5.2f} 超额{e['excess_annual']:+7.1%} "
                  f"IR{e['information_ratio']:+5.2f}", flush=True)
        out[wname]["deltas"] = {
            f"{b}_vs_PROD_SI": {"annual": out[wname][b]["annual"]
                                - out[wname]["PROD_SI"]["annual"],
                                "ir": out[wname][b]["ir"]
                                - out[wname]["PROD_SI"]["ir"],
                                "phase_excess_delta": [
                                    y["excess_annual"] - x["excess_annual"]
                                    for x, y in zip(
                                        out[wname]["PROD_SI"]["phase_detail"],
                                        out[wname][b]["phase_detail"])]}
            for b in ("LGBM_280", "LGBM_64", "GRU_64", "ML_MIX")}

    with open("reports/batch16_ab_20260910.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → reports/batch16_ab_20260910.json")


if __name__ == "__main__":
    main()
