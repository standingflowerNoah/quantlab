#!/usr/bin/env python3
"""PROD 池内 GRU 重排（池内精选路线，红利 DIV10 同款）
======================================================================
背景：batch16 证明 GRU 直接 top100 远逊 PROD（卫星稀释第四次复现），
但 GRU 因子层 IC 0.0911 极强。唯一未试组合方式：PROD 截面 top400 池内
按 GRU score 重排取 top100——在小市值+低流动性池内用时序信息精选。
对照：PROD / GRU 直接 / PROD_G400（池内重排）
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
    prod = build_composite(["size", "amihud_20", "sue_i", "overnight_mom_20"],
                           universe="ashare_ex")
    gru = load_score("gru_seq_score")
    pr = prod.copy()
    pr["date"] = pd.to_datetime(pr["date"])
    pool = pr.sort_values(["date", "score"], ascending=[True, False]).groupby(
        "date").head(400)
    sel = pool.merge(gru.rename(columns={"score": "gscore"}),
                     on=["date", "code"], how="left")
    sel = sel.dropna(subset=["gscore"])
    sel["score"] = sel["gscore"]
    g400 = sel[["date", "code", "score"]].copy()
    print(f"PROD_G400 覆盖 {g400['date'].nunique()} 日，"
          f"池内 GRU 覆盖率 {len(sel)/max(len(pool),1):.0%}", flush=True)

    scores = {"PROD_SI": prod, "GRU_64": gru, "PROD_G400": g400}
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
            for b in ("GRU_64", "PROD_G400")}

    with open("reports/batch17_gru_pool_20260910.json", "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
