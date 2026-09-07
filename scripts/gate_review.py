# -*- coding: utf-8 -*-
"""双闸门复核工具：纸面台账（signal_portfolio_multi）前向表现对比

用法：python scripts/gate_review.py [min_days]
对每个候选模型的每个记账快照，计算持仓加权前向 20 日收益（未满 20 日
的用已有天数折算），汇总均值/胜率，并与 PROD 同日配对差。
前向样本 < min_days 的模型标注"未满闸门"。2026-12 裁决时直接运行。
"""
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

import pandas as pd

from quantlab.data.store import Store

HORIZON = 20


def main(min_days: int = 60):
    store = Store()
    ledger = store.q("""
        SELECT model, date, code, weight FROM signal_portfolio_multi
        ORDER BY model, date""")
    if ledger.empty:
        print("账本为空")
        return
    ledger["date"] = pd.to_datetime(ledger["date"])

    px = store.q("""
        SELECT date, code, close * adj_factor AS c FROM kline_daily""")
    px["date"] = pd.to_datetime(px["date"])
    pmat = px.pivot(index="date", columns="code", values="c").sort_index()
    fwd20 = pmat.pct_change(HORIZON).shift(-HORIZON)  # t → t+20 收益

    rows = []
    for model, g in ledger.groupby("model"):
        dates = sorted(g["date"].unique())
        rets = []
        n_pending = 0
        for d in dates:
            h = g[g["date"] == d]
            if d not in fwd20.index:
                n_pending += 1
                continue
            fr = fwd20.loc[d].reindex(h["code"])
            w = h.set_index("code")["weight"]
            valid = fr.notna() & w.notna()
            if valid.sum() == 0:
                n_pending += 1
                continue
            rets.append({"date": d,
                         "ret": float((fr[valid] * w[valid]).sum()
                                      / w[valid].sum())})
        if not rets:
            print(f"{model}: {len(dates)} 个快照，前向 20 日均未到期"
                  f"（最新 {dates[-1].date()}），暂无纸面证据")
            continue
        r = pd.DataFrame(rets)
        rows.append({"model": model, "n_days": len(r),
                     "n_pending": n_pending,
                     "first": str(r["date"].min().date()),
                     "last": str(r["date"].max().date()),
                     "mean_fwd20": round(r["ret"].mean(), 5),
                     "pos_rate": round(float((r["ret"] > 0).mean()), 3),
                     "rets": r.set_index("date")["ret"]})

    if not rows:
        print("无有效前向样本（账本刚开始积累属正常，流水线逐日记账后自动可用）")
        return
    base = next((x for x in rows if x["model"] == "PROD"), None)
    print(f"{'模型':<10}{'快照数':>6}{'均值fwd20':>12}{'胜率':>8}  "
          f"{'vs PROD 配对差':>14}  闸门")
    for x in sorted(rows, key=lambda v: -v["mean_fwd20"]):
        diff = ""
        if base and x["model"] != "PROD":
            j = x["rets"].index.intersection(base["rets"].index)
            if len(j) > 0:
                d = (x["rets"].loc[j] - base["rets"].loc[j])
                diff = f"{d.mean()*100:+.2f}pp (n={len(j)}, 胜率{(d>0).mean()*100:.0f}%)"
        gate = "OK" if x["n_days"] >= min_days else \
            f"未满({x['n_days']}/{min_days})"
        pend = f"+{x['n_pending']}待到期" if x["n_pending"] else ""
        print(f"{x['model']:<10}{x['n_days']:>6}{pend:>4}"
              f"{x['mean_fwd20']*100:>11.2f}%"
              f"{x['pos_rate']*100:>7.0f}%  {diff:<16}  {gate}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 60)
