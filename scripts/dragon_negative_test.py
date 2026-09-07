"""dragon_net_20 负向信号实验（修复后 ICIR20 -1.14，上榜后均值回归）
用法1: size+amihud+dragon 三因子等权 rank（dragon 自动按 -1 方向）
用法2: 池内负向精选——size+amihud 选 400 候选，池内按 dragon 降序（避开游资净买入）取 100
对照:  size+amihud 纯基准
"""
import warnings
warnings.filterwarnings("ignore")
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import pandas as pd
from quantlab.model import build_composite, run_backtest
from quantlab.model.pool_select import build_pool_refined_score
from quantlab.config import get_logger

log = get_logger("dragon_neg")
START = "2022-01-05"

score_sa = build_composite(["size", "amihud_20"], universe="ashare_ex")
score_dg = build_composite(["dragon_net_20"], universe="ashare_ex")   # 已按 -1 方向

out = {}
# 1) 三因子等权
s3 = build_composite(["size", "amihud_20", "dragon_net_20"], universe="ashare_ex")
bt = run_backtest(s3, n_stocks=100, rebalance=20, start=START)
out["sa_dragon3f"] = {"metrics": bt["metrics"], "excess": bt["excess"],
                      "turnover": bt["turnover_avg"]}
# 2) 池内负向精选（refine 已是负向分数：值大=dragon 原值小=未被游资净买入）
sp = build_pool_refined_score(score_sa, score_dg, pool_n=400)
bt2 = run_backtest(sp, n_stocks=100, rebalance=20, start=START)
out["pool_neg_refine"] = {"metrics": bt2["metrics"], "excess": bt2["excess"],
                          "turnover": bt2["turnover_avg"]}
# 3) 对照
bt3 = run_backtest(score_sa, n_stocks=100, rebalance=20, start=START)
out["sa_ref"] = {"metrics": bt3["metrics"], "excess": bt3["excess"],
                 "turnover": bt3["turnover_avg"]}

for k, v in out.items():
    c = v["metrics"]; e = v["excess"]
    log.info(f"{k}: ann={c['annual_return']:+.1%} sharpe={c['sharpe']} "
             f"mdd={c['max_drawdown']:.1%} ir={e['information_ratio']} to={v['turnover']:.1%}")

# 分年度超额（池内负向精选 vs 对照）
years = [("2022", "2022-01-05", "2022-12-31"), ("2023", "2023-01-01", "2023-12-31"),
         ("2024", "2024-01-01", "2024-12-31"), ("2025", "2025-01-01", "2025-12-31"),
         ("2026", "2026-01-01", "2026-09-04")]
rows = []
for y, s, e in years:
    r1 = run_backtest(sp, n_stocks=100, rebalance=20, start=s, end=e)
    r2 = run_backtest(score_sa, n_stocks=100, rebalance=20, start=s, end=e)
    a1 = r1["metrics"]["annual_return"]; a2 = r2["metrics"]["annual_return"]
    rows.append({"year": y, "pool_neg": a1, "ref": a2, "excess": a1 - a2})
out["yearly"] = rows
json.dump(out, open("reports/dragon_negative_test.json", "w", encoding="utf-8"),
          ensure_ascii=False, default=str)

print()
for k in ("sa_ref", "sa_dragon3f", "pool_neg_refine"):
    c = out[k]["metrics"]; e = out[k]["excess"]
    print(f"{k:16s} 年化={c['annual_return']:+.1%} 夏普={c['sharpe']} "
          f"回撤={c['max_drawdown']:.1%} IR={e['information_ratio']} 换手={out[k]['turnover']:.1%}")
print()
print("分年度（池内负向精选 - 对照）:")
for r in rows:
    print(f"  {r['year']}: {r['pool_neg']:+.1%} vs {r['ref']:+.1%}  超额 {r['excess']:+.1%}")
