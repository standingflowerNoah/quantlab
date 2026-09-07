"""第二批胜出候选分层单调性检验（10 分组 × 未来20日收益）
目标: overnight_mom_20, chip_vwap_bias_250（冗余裁决参考: chip_age_short_10）
"""
import sys
sys.path.insert(0, '.')

import numpy as np
import pandas as pd

from quantlab.factor.quality import _fwd_return
from quantlab.data.store import Store

store = Store()
fwd = _fwd_return(20)

TARGETS = ["overnight_mom_20", "chip_vwap_bias_250", "chip_age_short_10"]
results = {}

for name in TARGETS:
    fv = store.read_factor(name)
    fv["date"] = pd.to_datetime(fv["date"])
    fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
    fv = fv[np.isfinite(fv["value"])]

    m = fv.merge(fwd, on=["date", "code"], how="inner")
    print(f"\n===== {name} =====")
    print(f"合并样本: {len(m)} 行, {m['date'].nunique()} 天, {m['code'].nunique()} 只")

    rows = []
    for d, g in m.groupby("date"):
        if len(g) < 100:
            continue
        q = pd.qcut(g["value"].rank(method="first"), q=10, labels=False)
        rows.append(g.assign(grp=q).groupby("grp")["fwd"].mean())
    dec = pd.DataFrame(rows)
    dec_mean = dec.mean() * 100
    print("10 分组平均未来20日收益（G1=最低 … G10=最高）:")
    print(dec_mean.round(3).to_string())
    ls = (dec[9].mean() - dec[0].mean()) * 100
    top = (dec[9].mean() - dec.mean().mean()) * 100
    print(f"多空（G10−G1）: {ls:+.3f}% / 20日")
    print(f"多头超额（G10−全均）: {top:+.3f}% / 20日")

    # 分年度多空
    m2 = m.copy()
    m2["year"] = m2["date"].dt.year
    yearly = {}
    for y, gy in m2.groupby("year"):
        sub_rows = []
        for d, g in gy.groupby("date"):
            if len(g) < 100:
                continue
            q = pd.qcut(g["value"].rank(method="first"), q=10, labels=False)
            sub_rows.append(g.assign(grp=q).groupby("grp")["fwd"].mean())
        dy = pd.DataFrame(sub_rows)
        if len(dy):
            yls = (dy[9].mean() - dy[0].mean()) * 100
            ytop = (dy[9].mean() - dy.mean().mean()) * 100
            yearly[y] = (yls, ytop, len(dy))
            print(f"  {y}: L-S {yls:+.3f}%  top-excess {ytop:+.3f}%  n={len(dy)}天")

    # 单调性: G1..G10 与组序号的 Spearman 相关
    from scipy.stats import spearmanr
    rho, p = spearmanr(dec_mean.index.values + 1, dec_mean.values)
    print(f"组序号-组收益 Spearman: rho={rho:+.3f} (p={p:.4f})")

    results[name] = {
        "deciles": dec_mean.round(4).to_dict(),
        "ls": round(ls, 4), "top": round(top, 4),
        "spearman": round(float(rho), 4),
        "yearly": {int(y): [round(v[0], 4), round(v[1], 4), int(v[2])] for y, v in yearly.items()},
    }

import json
with open("reports/batch2_layers_result.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("\n已写入 reports/batch2_layers_result.json")
