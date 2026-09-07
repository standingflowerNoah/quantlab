"""拥挤度动态门控回测：V3_GATE（w_t 动态）vs V3_FIX（固定 0.6）
======================================================================
门控规则（crowding.py 示意口径，本次首次回测验证）：
  w_t = 1 − 0.4 × pct_t，pct_t = 核心组（size+amihud_20）拥挤度的
  100 日因果滚动分位（核心越拥挤 → 核心组权重越低 → 新因子组让位）
组合：
  V3_FIX  = 0.6×rank(core) + 0.4×rank(sue_i+overnight)（固定 w，现基线）
  V3_GATE = w_t×rank(core) + (1−w_t)×rank(sue_i+overnight)（动态）
输出：reports/batch13_gate_20260907.json
"""
import sys
sys.path.insert(0, '.')

import json
import time
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from quantlab.model import build_composite, run_multiphase_backtest

N, REB, PHASES = 100, 20, 4
WINDOWS = {"focus": "2025-05-01", "full": "2022-07-01"}
END = "2026-09-04"


def mix_gate(core, new, w_map: dict | None, fixed_w: float = 0.6):
    """w_map: date→w_t（动态门控）；None 时用固定 w"""
    m = (core.rename(columns={"score": "s1"})
         .merge(new.rename(columns={"score": "s2"}), on=["date", "code"],
                how="inner"))
    m["r1"] = m.groupby("date")["s1"].rank(pct=True)
    m["r2"] = m.groupby("date")["s2"].rank(pct=True)
    if w_map is None:
        m["score"] = fixed_w * m["r1"] + (1 - fixed_w) * m["r2"]
    else:
        m["w"] = pd.to_datetime(m["date"]).map(w_map)
        m["w"] = m["w"].fillna(fixed_w)      # 门控序列缺失日用固定值兜底
        m["score"] = m["w"] * m["r1"] + (1 - m["w"]) * m["r2"]
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


def build_w_map() -> tuple[dict, dict]:
    """重放历史 w_t（与 monitor_pool 门控逻辑逐行一致）"""
    hist = pd.read_parquet("data/lake/crowding/history.parquet")
    core_names = ["size", "amihud_20"]
    series = []
    for n in core_names:
        g = (hist[hist["factor"] == n].set_index("date")["crowding"]
             .dropna())
        if len(g):
            series.append(g)
    core = pd.concat(series, axis=1).mean(axis=1).dropna()
    core.index = pd.to_datetime(core.index)
    pct = core.rolling(100, min_periods=40).apply(
        lambda a: float(np.mean(a <= a[-1])), raw=True).dropna()
    w = 1 - 0.4 * pct
    stats = {"min": float(w.min()), "max": float(w.max()),
             "mean": float(w.mean()), "start": str(w.index[0].date()),
             "end": str(w.index[-1].date())}
    return {d: float(v) for d, v in w.items()}, stats


def main():
    t0 = time.time()
    w_map, w_stats = build_w_map()
    print(f"门控序列 {len(w_map)} 日: w ∈ [{w_stats['min']:.2f}, "
          f"{w_stats['max']:.2f}] mean={w_stats['mean']:.2f} "
          f"({w_stats['start']} ~ {w_stats['end']})", flush=True)

    # 门控日频化：5 日采样 → 全交易日 ffill（对齐回测日频）
    from quantlab.data.store import Store
    _st = Store()
    all_td = pd.to_datetime(
        _st.q("SELECT DISTINCT CAST(date AS DATE) AS d FROM kline_daily "
              "ORDER BY d")["d"])
    w_s = pd.Series(w_map).sort_index()
    w_s.index = pd.to_datetime(w_s.index)
    w_daily = (w_s.reindex(all_td).ffill().dropna())
    w_map = {d: float(v) for d, v in w_daily.items()}
    print(f"日频化后 {len(w_map)} 日", flush=True)

    core = build_composite(["size", "amihud_20"], universe="ashare_ex")
    new = build_composite(["sue_i", "overnight_mom_20"], universe="ashare_ex")
    scores = {
        "V3_FIX": mix_gate(core, new, None, fixed_w=0.6),
        "V3_GATE": mix_gate(core, new, w_map),
    }
    for k, v in scores.items():
        print(f"  {k}: {len(v)} 行 {v['date'].nunique()} 天", flush=True)
    # 调试：map 命中率
    dbg = pd.to_datetime(scores["V3_GATE"]["date"]).map(w_map)
    print(f"  [调试] GATE w map 命中率: {dbg.notna().mean():.1%}，"
          f"非兜底 w 分布: {dbg.dropna().describe().round(3).to_dict()}", flush=True)

    out = {"config": {"n": N, "reb": REB, "phases": PHASES,
                      "windows": WINDOWS, "end": END, "w_stats": w_stats}}
    for wname, wstart in WINDOWS.items():
        out[wname] = {}
        for tag, sc in scores.items():
            r = run_multiphase_backtest(sc.copy(), n_stocks=N, rebalance=REB,
                                        phases=PHASES, start=wstart, end=END)
            out[wname][tag] = pack(r)
            m, e = r["metrics"], r["excess"]
            print(f"  {wname}/{tag:8s} 年化{m['annual_return']:+7.1%} "
                  f"夏普{m['sharpe']:5.2f} 超额{e['excess_annual']:+7.1%} "
                  f"IR{e['information_ratio']:+5.2f} "
                  f"回撤{m['max_drawdown']:+.1%}", flush=True)
        da, db = out[wname]["V3_FIX"], out[wname]["V3_GATE"]
        deltas = [y["excess_annual"] - x["excess_annual"]
                  for x, y in zip(da["phase_detail"], db["phase_detail"])]
        print(f"  ΔGATE_vs_FIX: 年化{db['annual']-da['annual']:+.1%} "
              f"IR{db['ir']-da['ir']:+.2f} 回撤变化"
              f"{db['mdd']-da['mdd']:+.1%} "
              f"相位超额Δ={['%+.1f%%' % (d*100) for d in deltas]}", flush=True)
        out[wname]["pair_deltas"] = {
            "GATE_vs_FIX": {
                "annual": db["annual"] - da["annual"],
                "ir": db["ir"] - da["ir"],
                "mdd": db["mdd"] - da["mdd"],
                "phase_excess_delta": deltas}}

    with open("reports/batch13_gate_20260907.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
