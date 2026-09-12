"""候选模型 A/B 全量重跑（新口径，2026-09-12）

背景：as-of 股本口径根治后，size 因子重算（rank 位移 7.2%、IC20 从
-0.0667 修正到 -0.0555）。旧口径下的候选模型 A/B 结论（PROD_HFA
71.7%/2.79 等）全部失效，2026-12 双闸门必须以新口径重跑。

模型口径与 scripts/daily_pipeline.py 的 TRACKED_MODELS 一致：
  PROD          size+amihud_20
  EQ3           核心+sue_i
  PROD_SI       核心+sue_i+overnight_mom_20
  V3_SI         0.6·rank(核心)+0.4·rank(sue_i+overnight)
  PROD_DUAL     0.9·rank(核心)+0.1·rank(4 风格卫星)
  PROD_HF       核心+hf_amihud_20
  PROD_HFA      核心+sue_i+hf_amihud_20
  EQ3_HFA_ICW   同 HFA 但 ICIR 加权（权重由 factor_icir_weights 现算）

窗口：sue_i 自 2025-04-18、hf_amihud_20 自 2025-01-22 起才有数据，
故含这两者的模型只能在 FOCUS(2025-05-01~) 窗口比较；PROD/PROD_DUAL
可跑全期 FULL(2022-07-01~)。END=2026-09-11。

参数：n=100 / reb=20 / inverse_vol / ashare_ex / 基准中证1000（引擎内建）。

输出: reports/candidate_ab_20260912.json + 控制台对照表
（旧口径参考数字取自 research/生产模型重构.md，仅作方向对照）
"""
from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports" / "candidate_ab_20260912.json"

FULL_START = "2022-07-01"
FOCUS_START = "2025-05-01"
END = "2026-09-11"

# 旧口径参考（research/生产模型重构.md 各轮记录，窗口口径见注释）
OLD_REF = {
    "PROD@FULL": {"annual": 0.367, "sharpe": 1.13, "mdd": -0.442, "window": FULL_START},
    "PROD@FOCUS": {"annual": 0.499, "sharpe": 1.95, "mdd": -0.256, "window": FOCUS_START},
    "EQ3@FOCUS": {"annual": None, "sharpe": None, "mdd": None, "window": FOCUS_START},
    "PROD_SI@FOCUS": {"annual": 0.572, "sharpe": 2.31, "mdd": -0.238, "window": FOCUS_START},
    "PROD_HF@FOCUS": {"annual": 0.655, "sharpe": 2.49, "mdd": -0.219, "window": FOCUS_START},
    "PROD_HFA@FOCUS": {"annual": 0.717, "sharpe": 2.79, "mdd": -0.207, "window": FOCUS_START},
    "EQ3_HFA_ICW@FOCUS": {"annual": 0.679, "sharpe": 2.82, "mdd": -0.170, "window": FOCUS_START},
}


def build_scores() -> dict:
    from quantlab.model import build_composite
    from quantlab.model.pool_select import build_dual_score
    from quantlab.model.composite import factor_coverage

    need = ["size", "amihud_20", "sue_i", "overnight_mom_20", "hf_amihud_20",
            "max_return_20", "amount_std_20", "ev_high_vol_20", "turnover_std_20"]
    cov = factor_coverage(need)
    bad = cov[~cov["ok"]]
    if len(bad):
        print("[warn] 因子水位异常（继续，但记录在案）:")
        print(bad.to_string(index=False))

    s = {}
    s["PROD@FULL"] = build_composite(["size", "amihud_20"],
                                     universe="ashare_ex", start=FULL_START)
    s["PROD_DUAL@FULL"] = build_dual_score()
    core = build_composite(["size", "amihud_20"], universe="ashare_ex")
    si = build_composite(["sue_i", "overnight_mom_20"], universe="ashare_ex")
    m = (core.rename(columns={"score": "s1"})
         .merge(si.rename(columns={"score": "s2"}), on=["date", "code"], how="inner"))
    m["r1"] = m.groupby("date")["s1"].rank(pct=True)
    m["r2"] = m.groupby("date")["s2"].rank(pct=True)
    v3 = m.assign(score=0.6 * m["r1"] + 0.4 * m["r2"])[["date", "code", "score"]]
    s["V3_SI@FOCUS"] = v3
    s["EQ3@FOCUS"] = build_composite(["size", "amihud_20", "sue_i"],
                                     universe="ashare_ex", start=FOCUS_START)
    s["PROD_SI@FOCUS"] = build_composite(
        ["size", "amihud_20", "sue_i", "overnight_mom_20"],
        universe="ashare_ex", start=FOCUS_START)
    s["PROD_HF@FOCUS"] = build_composite(["size", "amihud_20", "hf_amihud_20"],
                                         universe="ashare_ex", start=FOCUS_START)
    s["PROD_HFA@FOCUS"] = build_composite(
        ["size", "amihud_20", "sue_i", "hf_amihud_20"],
        universe="ashare_ex", start=FOCUS_START)
    s["EQ3_HFA_ICW@FOCUS"] = build_composite(
        ["size", "amihud_20", "sue_i", "hf_amihud_20"],
        universe="ashare_ex", method="ic_weighted", start=FOCUS_START)
    s["PROD@FOCUS"] = build_composite(["size", "amihud_20"],
                                      universe="ashare_ex", start=FOCUS_START)
    return s


def main() -> None:
    from quantlab.optimize.backtest import run_optimized_backtest

    t0 = time.time()
    scores = build_scores()
    print(f"评分构建完成 {time.time()-t0:.0f}s\n")

    res = {}
    rows = []
    for key, score in scores.items():
        start = FOCUS_START if key.endswith("@FOCUS") else FULL_START
        try:
            r = run_optimized_backtest(score.copy(), n_stocks=100, rebalance=20,
                                       method="inverse_vol", start=start, end=END)
        except Exception as e:
            print(f"  {key:22s} 回测失败: {str(e)[:90]}")
            continue
        m, ex = r["metrics"], r["excess"]
        c = r["curve"].copy()
        c["year"] = pd.to_datetime(c["date"]).dt.year
        yearly = {int(y): round(float((1 + g["ret"]).prod() - 1), 4)
                  for y, g in c.groupby("year")}
        res[key] = {"annual": round(m["annual_return"], 4),
                    "sharpe": round(m["sharpe"], 3),
                    "mdd": round(m["max_drawdown"], 4),
                    "excess": round(ex["excess_annual"], 4),
                    "ir": round(ex["information_ratio"], 3),
                    "turnover": round(r["turnover_avg"], 4),
                    "window": f"{start}~{END}", "yearly": yearly}
        old = OLD_REF.get(key, {})
        rows.append({
            "model": key,
            "annual": round(m["annual_return"] * 100, 1),
            "sharpe": round(m["sharpe"], 2),
            "mdd": round(m["max_drawdown"] * 100, 1),
            "excess": round(ex["excess_annual"] * 100, 1),
            "turnover": round(r["turnover_avg"] * 100, 0),
            "old_annual": (round(old["annual"] * 100, 1)
                           if old.get("annual") is not None else None),
            "old_sharpe": old.get("sharpe"),
        })
        print(f"  {key:22s} 年化 {m['annual_return']*100:6.1f}%  夏普 {m['sharpe']:5.2f}"
              f"  回撤 {m['max_drawdown']*100:6.1f}%  超额 {ex['excess_annual']*100:6.1f}%"
              f"  换手 {r['turnover_avg']*100:4.0f}%   (旧参考 "
              f"{old.get('annual') if old.get('annual') else '-'}"
              f"/{old.get('sharpe') or '-'})")

    df = pd.DataFrame(rows)
    print("\n=== 新口径候选模型对照表 ===")
    print(df.to_string(index=False))

    OUT.write_text(json.dumps({"generated": pd.Timestamp.now().isoformat(timespec="seconds"),
                               "window": {"FULL": f"{FULL_START}~{END}",
                                          "FOCUS": f"{FOCUS_START}~{END}"},
                               "results": res, "table": rows},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已写入 {OUT}（总耗时 {time.time()-t0:.0f}s）")


if __name__ == "__main__":
    main()
