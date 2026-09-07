"""估值增强专题 A/B：截面+时序双维估值信号
======================================================================
背景：epd_60/epds_60（EP 时序偏离）直加进 PROD_SI 失败（batch9），但
裁决指出其正确用法可能是【估值专题】——与截面估值组成双维信号。
本实验在估值场景下公平检验：
  BASE_E = [size, amihud_20, bp]           （现库估值暴露 = bp）
  TEST_E = [size, amihud_20, bp, epd_60, epds_60]（估值双维扩展）
对照：CORE = [size, amihud_20]（无估值暴露基线）
判定：TEST_E vs BASE_E 多相位配对。
输出：reports/batch10_ab_20260907.json
"""
import sys
sys.path.insert(0, '.')

import json
import time
import warnings
warnings.filterwarnings("ignore")

import pandas as pd

from quantlab.model import build_composite, run_multiphase_backtest

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


def main():
    t0 = time.time()
    scores = {
        "CORE": build_composite(["size", "amihud_20"], universe="ashare_ex"),
        "BASE_E": build_composite(["size", "amihud_20", "bp"],
                                  universe="ashare_ex"),
        "TEST_E": build_composite(["size", "amihud_20", "bp", "epd_60",
                                   "epds_60"], universe="ashare_ex"),
    }
    for k, v in scores.items():
        print(f"  {k}: {len(v)} 行 {v['date'].nunique()} 天", flush=True)

    pairs = {"E1_bp_gain": ("CORE", "BASE_E"),
             "E2_time_dim": ("BASE_E", "TEST_E")}
    out = {"config": {"n": N, "reb": REB, "phases": PHASES,
                      "windows": WINDOWS, "end": END, "pairs": pairs}}
    for wname, wstart in WINDOWS.items():
        out[wname] = {}
        for tag, sc in scores.items():
            r = run_multiphase_backtest(sc.copy(), n_stocks=N, rebalance=REB,
                                        phases=PHASES, start=wstart, end=END)
            out[wname][tag] = pack(r)
            m, e = r["metrics"], r["excess"]
            print(f"  {wname}/{tag:7s} 年化{m['annual_return']:+7.1%} "
                  f"夏普{m['sharpe']:5.2f} 超额{e['excess_annual']:+7.1%} "
                  f"IR{e['information_ratio']:+5.2f}", flush=True)
        for pname, (a, b) in pairs.items():
            da, db = out[wname][a], out[wname][b]
            deltas = [y["excess_annual"] - x["excess_annual"]
                      for x, y in zip(da["phase_detail"], db["phase_detail"])]
            print(f"  Δ{pname}: 年化{db['annual']-da['annual']:+.1%} "
                  f"IR{db['ir']-da['ir']:+.2f} "
                  f"相位超额Δ={['%+.1f%%' % (d*100) for d in deltas]}", flush=True)
        out[wname]["pair_deltas"] = {
            pname: {"annual": out[wname][b]["annual"] - out[wname][a]["annual"],
                    "ir": out[wname][b]["ir"] - out[wname][a]["ir"],
                    "phase_excess_delta": [
                        y["excess_annual"] - x["excess_annual"]
                        for x, y in zip(out[wname][a]["phase_detail"],
                                        out[wname][b]["phase_detail"])]}
            for pname, (a, b) in pairs.items()}

    with open("reports/batch10_ab_20260907.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → reports/batch10_ab_20260907.json")


if __name__ == "__main__":
    main()
