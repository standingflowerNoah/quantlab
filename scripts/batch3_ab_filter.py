"""补充实验 D：ev_high_vol_20 作为剔除项（而非打分项）
======================================================================
背景：实验 B/C 显示 ev_high_vol_20 等权打分直加对 PROD 组合伤害（focus
−6.5pp / full −5.0pp，4/4 相位负）。但其负 IC 逻辑是"高位放量=出货"，
更适合研报 A6 两融因子式的【剔除项】用法：
    PROD_FILTER = PROD 打分不变，但每截面剔除 ev_high_vol_20 排名前 20%
                  的股票（score − 1 沉底，等效剔出 top100 候选）
若剔除用法有效，说明其信息在"尾部风险过滤"而非"横截面打分"维度。
回测：run_multiphase_backtest 4 相位，top100、20日调仓、中证1000
"""
import sys
sys.path.insert(0, '.')

import json
import time
import warnings
warnings.filterwarnings("ignore")

import pandas as pd

from quantlab.data.store import Store
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
    store = Store()
    prod = build_composite(["size", "amihud_20"], universe="ashare_ex")
    ev = store.read_factor("ev_high_vol_20")
    ev["date"] = pd.to_datetime(ev["date"])
    ev["value"] = pd.to_numeric(ev["value"], errors="coerce")
    ev = ev.dropna(subset=["value"])

    m = prod.merge(ev.rename(columns={"value": "ev"}), on=["date", "code"],
                   how="left")
    m["ev_pct"] = m.groupby("date")["ev"].rank(pct=True)
    # 剔除 ev 排名前 20%（高位放量最极端的股票）：score − 1 沉底
    drop = m["ev_pct"] > 0.8
    m.loc[drop, "score"] = m.loc[drop, "score"] - 1.0
    filt = m[["date", "code", "score"]]
    n_drop = int(drop.sum())
    print(f"[过滤] 剔除标记 {n_drop} 行 / {len(m)} 行"
          f"（{n_drop/len(m):.1%}，含 ev 缺失自动保留）", flush=True)

    scores = {"PROD": prod, "PROD_FILTER": filt}
    out = {"config": {"n": N, "reb": REB, "phases": PHASES,
                      "windows": WINDOWS, "end": END,
                      "note": "剔除 ev_high_vol_20 截面前 20%（score-1 沉底）"}}
    for wname, wstart in WINDOWS.items():
        out[wname] = {}
        for tag, sc in scores.items():
            r = run_multiphase_backtest(sc.copy(), n_stocks=N, rebalance=REB,
                                        phases=PHASES, start=wstart, end=END)
            out[wname][tag] = pack(r)
            mm, e = r["metrics"], r["excess"]
            print(f"  {wname}/{tag:12s} 年化{mm['annual_return']:+7.1%} "
                  f"夏普{mm['sharpe']:5.2f} 超额{e['excess_annual']:+7.1%} "
                  f"IR{e['information_ratio']:+5.2f}", flush=True)
        a, b = out[wname]["PROD"], out[wname]["PROD_FILTER"]
        deltas = [y["excess_annual"] - x["excess_annual"]
                  for x, y in zip(a["phase_detail"], b["phase_detail"])]
        print(f"  ΔFILTER: 年化{b['annual']-a['annual']:+.1%} "
              f"IR{b['ir']-a['ir']:+.2f} "
              f"相位超额Δ={['%+.1f%%' % (d*100) for d in deltas]}", flush=True)
        out[wname]["delta"] = {
            "annual": b["annual"] - a["annual"], "ir": b["ir"] - a["ir"],
            "phase_excess_delta": deltas}

    with open("reports/batch3_ab_filter_20260907.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → reports/batch3_ab_filter_20260907.json")


if __name__ == "__main__":
    main()
