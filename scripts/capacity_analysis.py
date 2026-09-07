"""生产模型容量测算：当前持仓的流动性 vs 资金规模
经验法则：单股持仓市值 ≤ 该股日均成交额(ADV) 的 5~10% 时冲击成本可控。
输出：99 只持仓的 ADV 分布、给定资金规模下的占 ADV 比例、最大可容纳资金。
"""
import warnings
warnings.filterwarnings("ignore")
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import pandas as pd
from quantlab.data.store import query

cur = json.load(open("portfolio_state/current.json", encoding="utf-8"))
hold = pd.DataFrame(
    [{"code": c, "weight": w} for c, w in cur["holdings"].items()])
codes = ",".join(f"'{c}'" for c in hold["code"])

adv = query(f"""
    SELECT code, AVG(amount) AS adv
    FROM (SELECT code, date, amount FROM kline_daily
          WHERE code IN ({codes})
          ORDER BY date DESC LIMIT {len(codes) * 30})
    GROUP BY code
""")
m = hold.merge(adv, on="code", how="left")
m["adv_yi"] = m["adv"] / 1e8          # 亿元

print(f"=== 当前 {len(m)} 只持仓流动性（近 ~30 日日均成交额）===")
print(m["adv_yi"].describe(percentiles=[.1, .25, .5, .75, .9]).round(3).to_string())
print(f"合计 ADV: {m['adv_yi'].sum():.1f} 亿元 | 组合等权占比约 {1/len(m):.2%}")

print()
print("=== 不同资金规模下的单股持仓 / ADV（等权 ~1%）===")
for cap_yi in (500 / 1e4, 1000 / 1e4, 2000 / 1e4, 5000 / 1e4):   # 500万/1000万/2000万/5000万
    pos_yi = cap_yi / len(m)                      # 单股持仓（亿元）
    ratio = (pos_yi / m["adv_yi"]).replace([float("inf")], float("nan"))
    n_over = int((ratio > 0.10).sum())
    print(f"  资金 {cap_yi*1e4:>4.0f} 万: 单股 {pos_yi*1e4:>5.1f} 万 | "
          f"持仓/ADV 中位 {ratio.median():.2%} | 90分位 {ratio.quantile(.9):.2%} | "
          f"超10%红线个股 {n_over} 只")

# 最大可容纳资金：10% ADV 红线下，组合最弱 1% 权重个股决定上限
min_adv = m["adv_yi"].quantile(0.05)              # 5% 分位（最差 5 只附近）
cap_max = min_adv * 0.10 * len(m)
print()
print(f"最大可容纳资金（按最差 5% 持仓的 10% ADV 红线）：约 {cap_max*1e4:,.0f} 万元")
print(f"最大可容纳资金（按中位持仓股 10% ADV）：约 {m['adv_yi'].median()*0.10*len(m)*1e4:,.0f} 万元")
