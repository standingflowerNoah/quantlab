"""Alpha191 宽表因子 PIT 穿越抽查
抽样 top ICIR 因子，用「截断到 t0 的宽表」重算 t0 截面，与因子库全量版对比。
宽表 rolling 构造理论上只向后看，本脚本实证验证这一假设。
"""
import warnings
warnings.filterwarnings("ignore")
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
from quantlab.data.store import Store, query
from quantlab.factor.alpha191 import build_wide, _to_long
from quantlab.factor.alpha191_formulas import ALPHAS

# 抽样：ICIR20 最强的代表因子 + 一个 mid 置信度因子
SAMPLE = ["alpha013", "alpha016", "alpha055", "alpha126", "alpha101"]

store = Store()
wide_full = build_wide(store)
dates = wide_full["close"].index
t0s = [dates[int(len(dates) * p)] for p in (0.60, 0.85)]

all_fail = 0
for name in SAMPLE:
    fn = ALPHAS[name][0]
    res_full = _to_long(fn(wide_full))
    res_full["date"] = pd.to_datetime(res_full["date"])
    for t0 in t0s:
        wide_cut = {k: v.loc[:t0] for k, v in wide_full.items()}
        res_cut = _to_long(fn(wide_cut))
        res_cut["date"] = pd.to_datetime(res_cut["date"])
        a = (res_full[res_full["date"] == t0].set_index("code")["value"])
        b = (res_cut[res_cut["date"] == t0].set_index("code")["value"])
        common = a.index.union(b.index)
        va = a.reindex(common).fillna(0.0)
        vb = b.reindex(common).fillna(0.0)
        scale = va.abs().max() or 1.0
        n_diff = int(((va - vb).abs() > 1e-6 * max(scale, 1e-8)).sum())
        all_fail += n_diff
        tag = "PASS" if n_diff == 0 else f"FAIL({n_diff} 只差异)"
        print(f"{name} t0={t0.date()}: {tag}")
print()
print("结论:", "全部通过——Alpha191 宽表构造无穿越" if all_fail == 0
      else f"共 {all_fail} 处截面差异，需排查")
