"""第五批组合 A/B：SUE_I 同位替换（多相位口径）
======================================================================
sue 2026 拥挤衰减（拥挤度 96% 分位 + IC 0.049→0.011）背景下的改进检验：
  E1: V3（0.6×rank(size+amihud) + 0.4×rank(sue+overnight)）
      → V3_SI（sue 同位替换为 sue_i）
  E2: PROD 直接挂新因子组：
      PROD_S = 等权直加 [size,amihud,sue,overnight]
      PROD_SI = 等权直加 [size,amihud,sue_i,overnight]
回测：run_multiphase_backtest 4 相位，top100、20日调仓、扣费、中证1000
窗口：focus 2025-05-01（sue/sue_i 共同有效窗口）/ full 2022-07-01
注意：sue_i/sue_pred 数据 2025-04 起，full 窗口两者同缺早期——对照仍同位公平
输出：reports/batch5_ab_20260907.json
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


def mix_score(sa, sb, w):
    m = (sa.rename(columns={"score": "s1"})
         .merge(sb.rename(columns={"score": "s2"}), on=["date", "code"],
                how="inner"))
    m["r1"] = m.groupby("date")["s1"].rank(pct=True)
    m["r2"] = m.groupby("date")["s2"].rank(pct=True)
    m["score"] = w * m["r1"] + (1 - w) * m["r2"]
    return m[["date", "code", "score"]]


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
    print("[合成] 构建各变体因子分...", flush=True)
    core = build_composite(["size", "amihud_20"], universe="ashare_ex")
    new_sue = build_composite(["sue", "overnight_mom_20"], universe="ashare_ex")
    new_si = build_composite(["sue_i", "overnight_mom_20"], universe="ashare_ex")
    scores = {
        "V3": mix_score(core, new_sue, w=0.6),
        "V3_SI": mix_score(core, new_si, w=0.6),
        "PROD_S": build_composite(["size", "amihud_20", "sue",
                                   "overnight_mom_20"], universe="ashare_ex"),
        "PROD_SI": build_composite(["size", "amihud_20", "sue_i",
                                    "overnight_mom_20"], universe="ashare_ex"),
    }
    for k, v in scores.items():
        print(f"  {k}: {len(v)} 行 {v['date'].nunique()} 天", flush=True)

    pairs = {"E1_v3_swap": ("V3", "V3_SI"),
             "E2_prod_swap": ("PROD_S", "PROD_SI")}
    out = {"config": {"n": N, "reb": REB, "phases": PHASES,
                      "windows": WINDOWS, "end": END, "pairs": pairs}}
    for wname, wstart in WINDOWS.items():
        out[wname] = {}
        for tag, sc in scores.items():
            r = run_multiphase_backtest(sc.copy(), n_stocks=N, rebalance=REB,
                                        phases=PHASES, start=wstart, end=END)
            out[wname][tag] = pack(r)
            m, e = r["metrics"], r["excess"]
            print(f"  {wname}/{tag:8s} 年化{m['annual_return']:+7.1%} "
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

    with open("reports/batch5_ab_20260907.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → reports/batch5_ab_20260907.json")


if __name__ == "__main__":
    main()
