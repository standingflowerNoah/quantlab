"""龙虎榜事件池内建模：席位对偶因子的组合价值检验
======================================================================
背景：inst_buy_20（机构 h5 +0.034 五年全正）与 retail_buy_20（散户通道
ICIR −0.97）覆盖仅上榜股（~1200 只/20日窗口），无法进全市场合成——
但在【事件池内】它们是独立的排序信号。本实验在事件池内检验：
  EP_INST   = inst_buy_20（池内优选：机构建仓）
  EP_COMBO  = rank(inst_buy_20) − rank(retail_buy_20)（机构/散户对偶合成）
  EP_DRAGON = dragon_net_20（现有净买入总额对照）
top100（池内 ~1200 只可选）、20 日调仓、扣费、中证1000。
输出：reports/batch14_epool_20260907.json
"""
import sys
sys.path.insert(0, '.')

import json
import time
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from quantlab.data.store import Store
from quantlab.model import run_multiphase_backtest

N, REB, PHASES = 100, 20, 4
WINDOWS = {"focus": "2025-05-01", "full": "2022-07-01"}
END = "2026-09-04"


def yearly(curve):
    c = curve.copy()
    c["year"] = pd.to_datetime(c["date"]).dt.year
    return {int(y): round(float((1 + g["ret"]).prod() - 1), 4)
            for y, g in c.groupby("year")}


def pack(res):
    m, e = res["metrics"], res["excess"]
    return {
        "annual": m["annual_return"], "sharpe": m["sharpe"],
        "mdd": m["max_drawdown"], "excess": e["excess_annual"],
        "ir": e["information_ratio"], "yearly": yearly(res["curve"]),
        "phase_detail": res["phase_detail"],
    }


def load_factor(name):
    st = Store()
    fv = st.read_factor(name)
    fv["date"] = pd.to_datetime(fv["date"])
    fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
    fv = fv[np.isfinite(fv["value"])]
    fv["score"] = fv.groupby("date")["value"].rank(pct=True)
    return fv[["date", "code", "score"]]


def combo_score():
    inst = load_factor("inst_buy_20").rename(columns={"score": "r1"})
    ret = load_factor("retail_buy_20").rename(columns={"score": "r2"})
    m = inst.merge(ret, on=["date", "code"], how="inner")
    m["score"] = m["r1"] - m["r2"]          # 机构多 + 散户空
    return m[["date", "code", "score"]]


def main():
    t0 = time.time()
    scores = {
        "EP_INST": load_factor("inst_buy_20"),
        "EP_COMBO": combo_score(),
        "EP_DRAGON": load_factor("dragon_net_20"),
    }
    for k, v in scores.items():
        daily_cov = v.groupby("date")["code"].nunique()
        print(f"  {k}: {len(v)} 行，池均 {daily_cov.mean():.0f} 股/日 "
              f"({daily_cov.min()}~{daily_cov.max()})", flush=True)

    out = {"config": {"n": N, "reb": REB, "phases": PHASES,
                      "windows": WINDOWS, "end": END}}
    for wname, wstart in WINDOWS.items():
        out[wname] = {}
        for tag, sc in scores.items():
            r = run_multiphase_backtest(sc.copy(), n_stocks=N, rebalance=REB,
                                        phases=PHASES, start=wstart, end=END)
            out[wname][tag] = pack(r)
            m, e = r["metrics"], r["excess"]
            print(f"  {wname}/{tag:9s} 年化{m['annual_return']:+7.1%} "
                  f"夏普{m['sharpe']:5.2f} 超额{e['excess_annual']:+7.1%} "
                  f"IR{e['information_ratio']:+5.2f} "
                  f"回撤{m['max_drawdown']:+.1%}", flush=True)
        da, db = out[wname]["EP_DRAGON"], out[wname]["EP_COMBO"]
        deltas = [y["excess_annual"] - x["excess_annual"]
                  for x, y in zip(da["phase_detail"], db["phase_detail"])]
        print(f"  ΔCOMBO_vs_DRAGON: 年化{db['annual']-da['annual']:+.1%} "
              f"IR{db['ir']-da['ir']:+.2f} "
              f"相位超额Δ={['%+.1f%%' % (d*100) for d in deltas]}", flush=True)
        out[wname]["pair_deltas"] = {
            "COMBO_vs_DRAGON": {
                "annual": db["annual"] - da["annual"],
                "ir": db["ir"] - da["ir"],
                "phase_excess_delta": deltas}}

    with open("reports/batch14_epool_20260907.json", "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
