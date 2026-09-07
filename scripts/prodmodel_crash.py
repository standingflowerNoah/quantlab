# -*- coding: utf-8 -*-
"""极端情景压力测试：2024 年初微盘股灾 · 静态持仓回放

hf/sue 增量证据全部来自 2025-02+ 牛市窗口，唯一未被覆盖的尾部情景是
2024-01~02 微盘股灾。方法：取各模型当前最新目标持仓（权重固定），
回放股灾窗口的加权收益——衡量的是风格/选股暴露的尾部敏感度，
不是真实调仓路径（持仓在 2024 年会是另一批股票，但因子倾斜持续）。
产出 reports/prodmodel/crash2024.json
"""
import json
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

import pandas as pd

from quantlab.model import build_composite
from quantlab.decision import generate_target

OUT = "reports/prodmodel/crash2024.json"

MODELS = {
    "PROD": ["size", "amihud_20"],
    "EQ3": ["size", "amihud_20", "sue_i"],
    "HF_AMIH": ["size", "amihud_20", "hf_amihud_20"],
    "EQ3_HFA": ["size", "amihud_20", "sue_i", "hf_amihud_20"],
    "EQ3_HFA_ICW": None,  # 特殊：ICIR 加权变体单独构建
}

WINDOWS = {
    "crash_2024Q1": ("2023-12-29", "2024-02-05"),   # 峰值 → 谷底
    "crash_plus_rebound": ("2023-12-29", "2024-02-08"),
    "full_2024": ("2023-12-29", "2024-12-31"),
}


def main():
    from quantlab.data.store import Store
    store = Store()
    px = store.q("SELECT date, code, close * adj_factor AS c "
                 "FROM kline_daily WHERE date >= '2023-12-01'")
    px["date"] = pd.to_datetime(px["date"])
    pmat = px.pivot(index="date", columns="code", values="c").sort_index()

    idx = store.q("SELECT date, close FROM index_kline "
                  "WHERE code = '000852.SH'")
    idx["date"] = pd.to_datetime(idx["date"])
    bench = idx.set_index("date")["close"].sort_index()

    results = {"_note": "静态持仓回放：当前目标持仓权重固定，衡量风格暴露的尾部敏感度；"
                        "非真实调仓路径。基准=中证1000。"}
    for name, factors in MODELS.items():
        if factors is None:
            from quantlab.model.composite import FACTOR_DIRECTION
            score = _icw_score(["size", "amihud_20", "sue_i",
                                "hf_amihud_20"])
        else:
            score = build_composite(factors, universe="ashare_ex")
        tgt = generate_target(score.copy())
        w = tgt.set_index("code")["weight"]
        w = w[w > 0]
        res = {"n_holdings": int(len(w))}
        for wname, (d0, d1) in WINDOWS.items():
            p0, p1 = pd.Timestamp(d0), pd.Timestamp(d1)
            seg = pmat.loc[p0:p1]
            base = seg.iloc[0]
            rel = seg.iloc[-1] / base - 1
            common = w.index.intersection(rel.dropna().index)
            wr = rel.loc[common]
            ww = w.loc[common] / w.loc[common].sum()
            port = float((wr * ww).sum())
            b = bench.loc[p0:p1]
            bench_ret = float(b.iloc[-1] / b.iloc[0] - 1)
            # 持仓等权对照（剔除权重影响，纯选股暴露）
            eq = float(wr.mean())
            res[wname] = {"port": round(port, 4),
                          "equal": round(eq, 4),
                          "bench": round(bench_ret, 4)}
        results[name] = res
        print(name, json.dumps(res), flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print("saved", OUT, flush=True)


def _icw_score(factors):
    """ICIR 加权合成（与 build_composite method='ic_weighted' 同路径）"""
    from quantlab.model import build_composite
    return build_composite(factors, universe="ashare_ex",
                           method="ic_weighted")


if __name__ == "__main__":
    main()
