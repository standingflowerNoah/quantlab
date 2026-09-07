"""第四批组合 A/B：holder_num_chg_2 增量检验（多相位口径）
======================================================================
B1 卖点是全新信息源（与全库最大冗余仅 0.15），必须过组合关：
  D1: PROD=[size,amihud] → PROD_HN=[size,amihud,holder_num_chg_2] 等权直加
  D2: V3（0.6核心+0.4新因子组）新因子组内加 holder_num_chg_2
回测：run_multiphase_backtest 4 相位，top100、20日调仓、扣费、中证1000
窗口：focus 2025-05-01 / full 2022-07-01，END 2026-09-04
输出：reports/batch4_ab_20260907.json
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
    prod_hn = build_composite(["size", "amihud_20", "holder_num_chg_2"],
                              universe="ashare_ex")
    new2 = build_composite(["sue", "overnight_mom_20"], universe="ashare_ex")
    new3 = build_composite(["sue", "overnight_mom_20", "holder_num_chg_2"],
                           universe="ashare_ex")
    scores = {
        "PROD": core, "PROD_HN": prod_hn,
        "V3": mix_score(core, new2, w=0.6),
        "V3_HN": mix_score(core, new3, w=0.6),
    }
    for k, v in scores.items():
        print(f"  {k}: {len(v)} 行 {v['date'].nunique()} 天", flush=True)

    pairs = {"D1_prod_add": ("PROD", "PROD_HN"),
             "D2_v3_add": ("V3", "V3_HN")}
    out = {"config": {"n": N, "reb": REB, "phases": PHASES,
                      "windows": WINDOWS, "end": END, "pairs": pairs}}
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

    with open("reports/batch4_ab_20260907.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → reports/batch4_ab_20260907.json")


if __name__ == "__main__":
    main()
