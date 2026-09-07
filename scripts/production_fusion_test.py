"""生产模型融合实验：size+amihud 核心如何引入 sue + overnight_mom_20
======================================================================
背景：生产模型已于 2026-09-05 回退为纯 size+amihud 两因子（dragon_net_20
     前视修复后为负 IC 移出精选）。本实验检验新因子的三种接入方式：
  PROD = [size, amihud_20]（当前生产）
  V1   = [size, amihud_20, sue, overnight_mom_20]（等权直加——检验核心暴露稀释）
  V2   = 池内精选：size+amihud 选 400 → sue+overnight 池内二次排序
  V3   = 加权融合：0.6×rank(size+amihud) + 0.4×rank(sue+overnight)
回测：run_multiphase_backtest（4 相位平均，平滑网格运气），top100、20日调仓、扣费、中证1000
窗口：focus 2025-05-01（sue 生效后）+ full 2022-07-01（参考）
输出：reports/production_fusion_20260906.json
"""
import sys
sys.path.insert(0, '.')

import json
import time
import warnings
warnings.filterwarnings("ignore")

import pandas as pd

from quantlab.model import build_composite, run_multiphase_backtest
from quantlab.model.pool_select import build_pool_refined_score

CORE = ["size", "amihud_20"]
NEW = ["sue", "overnight_mom_20"]
N, REB, PHASES = 100, 20, 4
WINDOWS = {"focus": "2025-05-01", "full": "2022-07-01"}
END = "2026-09-04"


def mix_score(sa, sb, w):
    """w×rank(sa) + (1−w)×rank(sb)（逐日截面重排名后融合）"""
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


def pack(res, with_curve=True):
    m, e = res["metrics"], res["excess"]
    out = {
        "annual": m["annual_return"], "sharpe": m["sharpe"],
        "mdd": m["max_drawdown"], "calmar": m["calmar"],
        "excess": e["excess_annual"], "ir": e["information_ratio"],
        "win": e["win_rate_vs_bm"], "turnover": res["turnover_avg"],
        "yearly": yearly(res["curve"]),
        "phase_detail": res["phase_detail"],
    }
    if with_curve:
        out["dates"] = [str(pd.Timestamp(d).date()) for d in res["curve"]["date"]]
        out["nav"] = [round(float(v), 4) for v in res["curve"]["nav"]]
        out["nav_bm"] = [round(float(v), 4) for v in res["curve"]["nav_bm"]]
    return out


def main():
    t0 = time.time()
    core = build_composite(CORE, universe="ashare_ex")
    new = build_composite(NEW, universe="ashare_ex")
    v1 = build_composite(CORE + NEW, universe="ashare_ex")
    v2 = build_pool_refined_score(core, new, pool_n=400)
    v3 = mix_score(core, new, w=0.6)
    scores = {"prod": core, "v1": v1, "v2": v2, "v3": v3}
    for k, v in scores.items():
        print(f"[合成 {k}] {len(v)} 行, {v['date'].nunique()} 天")

    out = {"config": {"core": CORE, "new": NEW, "n": N, "reb": REB,
                      "phases": PHASES, "windows": WINDOWS, "end": END,
                      "note": "V2=池内精选(pool 400); V3=0.6/0.4 加权融合"}}
    for wname, wstart in WINDOWS.items():
        out[wname] = {}
        for tag, sc in scores.items():
            r = run_multiphase_backtest(sc.copy(), n_stocks=N, rebalance=REB,
                                        phases=PHASES, start=wstart, end=END)
            out[wname][tag] = pack(r)
            m, e = r["metrics"], r["excess"]
            pa = [p["annual_return"] for p in r["phase_detail"]]
            print(f"  {wname}/{tag:4s} 年化{m['annual_return']:+7.1%} "
                  f"夏普{m['sharpe']:5.2f} 回撤{m['max_drawdown']:6.1%} "
                  f"超额{e['excess_annual']:+7.1%} IR{e['information_ratio']:+5.2f} "
                  f"| 单相位年化区间 [{min(pa):+.1%}, {max(pa):+.1%}]")
        d = out[wname]
        print(f"  {wname} Δ: V1{d['v1']['annual']-d['prod']['annual']:+.1%} "
              f"V2{d['v2']['annual']-d['prod']['annual']:+.1%} "
              f"V3{d['v3']['annual']-d['prod']['annual']:+.1%}")

    with open("reports/production_fusion_20260906.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → reports/production_fusion_20260906.json")


if __name__ == "__main__":
    main()
