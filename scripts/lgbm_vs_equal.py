"""全因子 LightGBM 模型 vs 等权基线 多相位回测对比
======================================================================
LGBM：258 因子（251 过覆盖率筛选）walk-forward 滚动训练的 ML 合成分
PROD_SI：等权最优直加组 [size, amihud_20, sue_i, overnight_mom_20]
CORE：[size, amihud_20] 生产核心
LGBM_E：251 因子中挑等权可用的（过度实验，略）
判定：LGBM 相对 PROD_SI 的多相位配对增量。
输出：reports/lgbm_vs_equal_20260907.json
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
END = "2026-08-06"          # LGBM score 覆盖终点（标签 T+21 截断）


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


def load_lgbm_score():
    fv = pd.read_parquet("data/lake/factor/lgbm_all_score/part-b00001.parquet")
    fv["date"] = pd.to_datetime(fv["date"])
    # 截面 rank pct 归一（与等权合成同尺度语义）
    fv["score"] = fv.groupby("date")["score"].rank(pct=True)
    return fv[["date", "code", "score"]]


def main():
    t0 = time.time()
    scores = {
        "LGBM_ALL": load_lgbm_score(),
        "PROD_SI": build_composite(["size", "amihud_20", "sue_i",
                                    "overnight_mom_20"],
                                   universe="ashare_ex"),
        "CORE": build_composite(["size", "amihud_20"], universe="ashare_ex"),
    }
    for k, v in scores.items():
        print(f"  {k}: {len(v)} 行 {v['date'].nunique()} 天", flush=True)

    out = {"config": {"n": N, "reb": REB, "phases": PHASES,
                      "windows": WINDOWS, "end": END}}
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
        for pname, (a, b) in {"ML_vs_PROD_SI": ("PROD_SI", "LGBM_ALL"),
                              "ML_vs_CORE": ("CORE", "LGBM_ALL")}.items():
            da, db = out[wname][a], out[wname][b]
            deltas = [y["excess_annual"] - x["excess_annual"]
                      for x, y in zip(da["phase_detail"], db["phase_detail"])]
            print(f"  Δ{pname}: 年化{db['annual']-da['annual']:+.1%} "
                  f"IR{db['ir']-da['ir']:+.2f} "
                  f"相位超额Δ={['%+.1f%%' % (d*100) for d in deltas]}", flush=True)

    with open("reports/lgbm_vs_equal_20260907.json", "w",
              encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
