"""第三批组合 A/B：分域替换 + 事件簇增量（多相位口径）
======================================================================
实验 A（分域替换）：M_ep  = [size, amihud_20, ep]
                    M_dom = [size, amihud_20, ep_size_dom]（同因子数同位替换）
实验 B（事件簇增量）：PROD    = [size, amihud_20]（当前生产）
                      PROD_EV = [size, amihud_20, ev_high_vol_20]（等权直加）
实验 C（新因子边际）：V3 = 0.6×rank(size+amihud) + 0.4×rank(sue+overnight_mom_20)
                      V3_EV = 同 V3 但新因子组加 ev_high_vol_20
回测：run_multiphase_backtest（4 相位平均），top100、20日调仓、扣费、中证1000
窗口：focus 2025-05-01 / full 2022-07-01（参考），END 2026-09-04
判定：跨窗口配对相位 Δ 全正才支持替换/加入
输出：reports/batch3_ab_20260907.json
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
        "ir": e["information_ratio"], "turnover": res["turnover_avg"],
        "yearly": yearly(res["curve"]),
        "phase_detail": res["phase_detail"],
    }


def main():
    t0 = time.time()
    print("[合成] 构建各变体因子分...", flush=True)
    size_ami = build_composite(["size", "amihud_20"], universe="ashare_ex")
    m_ep = build_composite(["size", "amihud_20", "ep"], universe="ashare_ex")
    m_dom = build_composite(["size", "amihud_20", "ep_size_dom"],
                            universe="ashare_ex")
    prod_ev = build_composite(["size", "amihud_20", "ev_high_vol_20"],
                              universe="ashare_ex")
    new3 = build_composite(["sue", "overnight_mom_20", "ev_high_vol_20"],
                           universe="ashare_ex")
    new2 = build_composite(["sue", "overnight_mom_20"], universe="ashare_ex")
    v3 = mix_score(size_ami, new2, w=0.6)
    v3_ev = mix_score(size_ami, new3, w=0.6)

    scores = {
        "M_ep": m_ep, "M_dom": m_dom,
        "PROD": size_ami, "PROD_EV": prod_ev,
        "V3": v3, "V3_EV": v3_ev,
    }
    for k, v in scores.items():
        print(f"  {k}: {len(v)} 行 {v['date'].nunique()} 天", flush=True)

    pairs = {"A_dom_swap": ("M_ep", "M_dom"),
             "B_ev_add": ("PROD", "PROD_EV"),
             "C_v3_ev": ("V3", "V3_EV")}

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
            da = out[wname][a]
            db = out[wname][b]
            pa = [p["excess_annual"] for p in da["phase_detail"]]
            pb = [p["excess_annual"] for p in db["phase_detail"]]
            deltas = [y - x for x, y in zip(pa, pb)]
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

    with open("reports/batch3_ab_20260907.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → reports/batch3_ab_20260907.json")


if __name__ == "__main__":
    main()
