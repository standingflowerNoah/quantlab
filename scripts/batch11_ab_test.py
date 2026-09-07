"""ML 筛选因子子集组合检验：LightGBM 重要性 top20 等权 vs 人工组合
======================================================================
上轮结论：全因子 ML 组合惨败（头部区分力被平均化），但特征重要性是
独立证据源。本实验检验：ML 筛出的 top20 因子做等权组合，能否胜过
人工挑选的 PROD_SI？——判定 ML 因子选择能力的组合价值。
  LGBM_TOP20：重要性 top20 等权（覆盖参差——事件因子仅上榜股有值）
  LGBM_COV95：top20 中覆盖率≥95% 的子集等权（排除事件稀疏因子）
  PROD_SI / CORE：人工基线
END 对齐 2026-08-06（LGBM score 覆盖终点），全窗口公平。
输出：reports/batch11_ab_20260907.json
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
WINDOWS = {"focus": "2025-05-01", "full": "2024-01-01"}
END = "2026-08-06"

TOP20 = ["lockup_pressure_60", "ev_low_vol_20", "block_premium_20",
         "dragon_net_20", "ev_high_vol_20", "total_mcap", "size",
         "alpha010", "asset_turnover", "revenue_growth_yoy", "alpha040",
         "ep", "earnings_accel", "asset_growth_yoy", "bp",
         "price_position_250", "epds_60", "profit_growth_yoy",
         "amihud_20", "alpha024"]
COV95 = [f for f in TOP20 if f not in
         ("lockup_pressure_60", "block_premium_20", "dragon_net_20",
          "ev_high_vol_20", "ev_low_vol_20")]      # 事件因子覆盖稀疏


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
    print(f"COV95 子集（排除事件稀疏因子）: {COV95}", flush=True)
    scores = {
        "PROD_SI": build_composite(["size", "amihud_20", "sue_i",
                                    "overnight_mom_20"],
                                   universe="ashare_ex"),
        "CORE": build_composite(["size", "amihud_20"], universe="ashare_ex"),
        "LGBM_TOP20": build_composite(TOP20, universe="ashare_ex"),
        "LGBM_COV95": build_composite(COV95, universe="ashare_ex"),
    }
    for k, v in scores.items():
        print(f"  {k}: {len(v)} 行 {v['date'].nunique()} 天", flush=True)

    pairs = {"T20_vs_PROD": ("PROD_SI", "LGBM_TOP20"),
             "C95_vs_PROD": ("PROD_SI", "LGBM_COV95")}
    out = {"config": {"n": N, "reb": REB, "phases": PHASES,
                      "windows": WINDOWS, "end": END, "pairs": pairs}}
    for wname, wstart in WINDOWS.items():
        out[wname] = {}
        for tag, sc in scores.items():
            r = run_multiphase_backtest(sc.copy(), n_stocks=N, rebalance=REB,
                                        phases=PHASES, start=wstart, end=END)
            out[wname][tag] = pack(r)
            m, e = r["metrics"], r["excess"]
            print(f"  {wname}/{tag:10s} 年化{m['annual_return']:+7.1%} "
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

    with open("reports/batch11_ab_20260907.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
