"""校验 xiaodefa moneyflow 回补数据质量：与东财探针(主库 fund_flow_daily)交叉比对。

东财 push2his 单位=元；xiaodefa moneyflow 单位=万元。
比对口径：main_net(东财,元) vs (super_net+large_net)*1e4(万元→元)
容差：东财与小reditors源分单口径存在差异（主动买卖判定标准不同），
比值中位数应接近 1，允许 ±30% 离散度；一致性以 rank 相关（方向一致性）为准。
"""
import duckdb
import numpy as np
import pandas as pd
from pathlib import Path
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
MF = ROOT / "data" / "lake" / "clean" / "moneyflow"

con = duckdb.connect(str(ROOT / "data" / "quant.duckdb"), read_only=True)
em = con.execute("""
    select date, code, main_net from fund_flow_daily
""").fetchdf()
con.close()

files = sorted(MF.glob("part-*.parquet"))
c2 = duckdb.connect()
fs = ", ".join(f"'{f.as_posix()}'" for f in files)
xd = c2.execute(f"""
    select date, code, (super_net + large_net) * 10000 as main_net_yuan,
           count(*) over () as n_total
    from read_parquet([{fs}], hive_partitioning=false)
""").fetchdf()
c2.close()

print(f"东财探针 {len(em)} 行（{em['code'].nunique()} 只）；xiaodefa {len(xd):,} 行")
m = em.merge(xd, on=["date", "code"])
print(f"重叠 join: {len(m)} 行 / {m['code'].nunique()} 只")
m = m.rename(columns={"main_net": "main_net_em"}).dropna(subset=["main_net_em", "main_net_yuan"])
m = m[m["main_net_em"].abs() > 1e6]  # 排除极小值
ratio = m["main_net_em"] / m["main_net_yuan"]
print(f"\n主力净流入比值(东财/xd): median={ratio.median():.3f} "
      f"p25={ratio.quantile(0.25):.3f} p75={ratio.quantile(0.75):.3f}")
same_sign = (np.sign(m["main_net_em"]) == np.sign(m["main_net_yuan"])).mean()
rho = stats.spearmanr(m["main_net_em"], m["main_net_yuan"]).statistic
print(f"方向一致率: {same_sign:.1%}；Spearman rho: {rho:.3f}")

# 覆盖检查：事件表 join 覆盖率
ev = pd.read_parquet(ROOT / "reports/_tmp/abnormal_events.parquet",
                     columns=["date", "code"])
ev["date"] = pd.to_datetime(ev["date"])
j = ev.merge(xd[["date", "code"]], on=["date", "code"], how="inner")
print(f"\n事件资金流覆盖率: {len(j):,}/{len(ev):,} = {len(j)/len(ev):.1%}")

# 分年覆盖
xd["date"] = pd.to_datetime(xd["date"])
g = xd.groupby(xd["date"].dt.year).agg(days=("date", "nunique"), rows=("code", "size"))
print("\nxiaodefa 资金流分年覆盖：")
print(g.to_string())
