#!/usr/bin/env python3
"""PB-ROE 三重过滤组合层 A/B（2026-09-10）
======================================================================
问题：生产核心 size+amihud_20 叠加「低 PB + 高 ROE」质量估值维度，
能否在组合层带来增量？（因子层 pb/roe 均为 PIT 干净的多期版）
三条对照路径（同回测引擎、同窗口、n=100/20 日调仓/扣费/中证1000）：
  PROD        size+amihud_20 等权 rank（生产现役）
  EQ3         +sue_i（切换候选）
  PROD_PBROE  size+amihud_20+bp+roe 四分量等权 rank（直接加维度）
  PROD_F400   PROD 截面 top400 池内按 (roe↑, bp↓) 重排取 top100（池内精选）
判读口径与 batch12 一致：focus + full 双窗口、4 相位平滑。
"""
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import json

import numpy as np
import pandas as pd

from quantlab.data.store import Store
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


def load_fv(name):
    store = Store(readonly=True)
    return store.read_factor(name)


def main():
    t0 = time.time()
    prod = build_composite(["size", "amihud_20"], universe="ashare_ex")
    eq3 = build_composite(["size", "amihud_20", "sue_i"], universe="ashare_ex")
    pbroe4 = build_composite(["size", "amihud_20", "bp", "roe"],
                             universe="ashare_ex")

    # 池内精选：PROD top400 → 池内 (roe↑ + (1-bp↑)) 重排 top100
    roe = load_fv("roe")
    bp = load_fv("bp")
    for d in (roe, bp):
        d["date"] = pd.to_datetime(d["date"])
    rp = roe.dropna().copy()
    rp["rk"] = rp.groupby("date")["value"].rank(pct=True)
    bpv = bp.dropna().copy()
    bpv["rk"] = 1 - bpv.groupby("date")["value"].rank(pct=True)
    qual = rp.merge(bpv, on=["date", "code"], suffixes=("", "_bp"))
    qual["q"] = qual["rk"] + qual["rk_bp"]
    pr = prod.copy()
    pr["date"] = pd.to_datetime(pr["date"])
    pr["pr"] = pr.groupby("date")["score"].rank(pct=True)
    pool = pr.sort_values(["date", "score"], ascending=[True, False]).groupby(
        "date").head(400)
    sel = pool.merge(qual[["date", "code", "q"]], on=["date", "code"],
                     how="left")
    sel["score"] = sel["q"]
    sel = sel.dropna(subset=["score"])
    filt = sel[["date", "code", "score"]].copy()
    print(f"PROD_F400 覆盖 {filt['date'].nunique()} 日", flush=True)

    scores = {"PROD": prod, "EQ3": eq3, "PROD_PBROE": pbroe4,
              "PROD_F400": filt}
    out = {"config": {"n": N, "reb": REB, "end": END, "windows": WINDOWS}}
    for wname, wstart in WINDOWS.items():
        out[wname] = {}
        for tag, sc in scores.items():
            r = run_multiphase_backtest(sc.copy(), n_stocks=N, rebalance=REB,
                                        phases=PHASES, start=wstart, end=END)
            out[wname][tag] = pack(r)
            m, e = r["metrics"], r["excess"]
            print(f"  {wname}/{tag:11s} 年化{m['annual_return']:+7.1%} "
                  f"夏普{m['sharpe']:5.2f} 超额{e['excess_annual']:+7.1%} "
                  f"IR{e['information_ratio']:+5.2f}", flush=True)
        out[wname]["deltas"] = {
            f"{b}_vs_PROD": {"annual": out[wname][b]["annual"]
                             - out[wname]["PROD"]["annual"],
                             "ir": out[wname][b]["ir"] - out[wname]["PROD"]["ir"],
                             "phase_excess_delta": [
                                 y["excess_annual"] - x["excess_annual"]
                                 for x, y in zip(out[wname]["PROD"]["phase_detail"],
                                                 out[wname][b]["phase_detail"])]}
            for b in ("EQ3", "PROD_PBROE", "PROD_F400")}

    with open("reports/pbroe_ab_20260910.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → reports/pbroe_ab_20260910.json")


if __name__ == "__main__":
    main()
