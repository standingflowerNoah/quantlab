"""拥挤度监控：监控池全量跑批 → reports/crowding_monitor_20260906.json
====================================================================
数据层复用 quantlab.factor.crowding.monitor_pool（流水线同源），
本脚本只做研究侧 JSON 组装（供 HTML 报告生成器消费）。
"""
import sys
sys.path.insert(0, '.')

import json
import time
import warnings
warnings.filterwarnings("ignore")

import pandas as pd

from quantlab.factor.crowding import monitor_pool
from quantlab.model.composite import FACTOR_DIRECTION


def main():
    t0 = time.time()
    df_long, alerts, gate = monitor_pool()

    out = {"pool": json.load(open("data/lake/crowding/latest.json",
                                  encoding="utf-8"))["pool"],
           "series": {}, "latest": {}}
    for n, g in df_long.groupby("factor"):
        g = g.sort_values("date")
        ch = g.dropna(subset=["crowding"])
        latest = ch.iloc[-1] if len(ch) else None
        out["series"][n] = {
            "group": str(g["group"].iloc[0]),
            "direction": FACTOR_DIRECTION.get(n, 1),
            "dates": [str(pd.Timestamp(d).date()) for d in g["date"]],
            "crowding": [round(float(v), 3) if pd.notna(v) else None
                         for v in g["crowding"]],
            "val_z": [round(float(v), 3) if pd.notna(v) else None
                      for v in g["val_z"]],
            "corr_z": [round(float(v), 3) if pd.notna(v) else None
                       for v in g["corr_z"]],
            "turnover_pct": [round(float(v), 3) if pd.notna(v) else None
                             for v in g["turnover_pct"]],
        }
        if latest is not None:
            out["latest"][n] = {
                "date": str(pd.Timestamp(latest["date"]).date()),
                "crowding": round(float(latest["crowding"]), 3),
                "val_z": None if pd.isna(latest["val_z"]) else round(float(latest["val_z"]), 3),
                "corr_z": None if pd.isna(latest["corr_z"]) else round(float(latest["corr_z"]), 3),
                "turnover_pct": None if pd.isna(latest["turnover_pct"])
                                else round(float(latest["turnover_pct"]), 3),
                "hist_pct": round(float((ch["crowding"] <= latest["crowding"]).mean()), 3),
            }
            print(f"  {n:20s} 最新 crowding {out['latest'][n]['crowding']:+.2f} "
                  f"(历史分位 {out['latest'][n]['hist_pct']:.0%})")

    out["gate"] = gate
    if gate.get("w_current") is not None:
        print(f"\n[门控] 核心拥挤分位 {gate['pct_current']:.0%} → "
              f"w = {gate['w_current']:.2f}")
    if alerts:
        print(f"[预警] {alerts}")

    with open("reports/crowding_monitor_20260906.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → reports/crowding_monitor_20260906.json")


if __name__ == "__main__":
    main()
