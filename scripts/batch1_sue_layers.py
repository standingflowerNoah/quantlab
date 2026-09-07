"""SUE 分层单调性检验（10 分组 × 未来20日收益，简化分层回测）"""
import sys
sys.path.insert(0, '.')

import numpy as np
import pandas as pd

from quantlab.factor.quality import _fwd_return
from quantlab.data.store import Store

store = Store()
fv = store.read_factor("sue")
fv["date"] = pd.to_datetime(fv["date"])
fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
fv = fv[np.isfinite(fv["value"])]

fwd = _fwd_return(20)
m = fv.merge(fwd, on=["date", "code"], how="inner")
print(f"合并样本: {len(m)} 行, {m['date'].nunique()} 天, {m['code'].nunique()} 只")

# 逐日 10 分组（rank 均分），组内等权平均 fwd
bins = np.arange(0, 11) / 10
rows = []
for d, g in m.groupby("date"):
    if len(g) < 100:
        continue
    q = pd.qcut(g["value"].rank(method="first"), q=10, labels=False)
    g = g.assign(grp=q)
    rows.append(g.groupby("grp")["fwd"].mean())
dec = pd.DataFrame(rows)
print("\n10 分组平均未来20日收益（G1=最低SUE … G10=最高SUE）:")
print((dec.mean() * 100).round(3).to_string())
print("\n多空（G10−G1）: {:.3f}% / 20日".format((dec[9].mean() - dec[0].mean()) * 100))
print("多头超额（G10−全均）: {:.3f}% / 20日".format((dec[9].mean() - dec.mean().mean()) * 100))
print("\n分年度多空（G10−G1, %/20日）:")
m2 = m.copy()
m2["year"] = m2["date"].dt.year
for y, gy in m2.groupby("year"):
    sub_rows = []
    for d, g in gy.groupby("date"):
        if len(g) < 100:
            continue
        q = pd.qcut(g["value"].rank(method="first"), q=10, labels=False)
        g = g.assign(grp=q)
        sub_rows.append(g.groupby("grp")["fwd"].mean())
    dy = pd.DataFrame(sub_rows)
    if len(dy):
        print(f"  {y}: L-S {(dy[9].mean()-dy[0].mean())*100:+.3f}%  "
              f"top-excess {(dy[9].mean()-dy.mean().mean())*100:+.3f}%  n={len(dy)}天")
