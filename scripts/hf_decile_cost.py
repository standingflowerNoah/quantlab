"""10 分位切细 + 费用敏感性检验：高频因子费后还剩多少
======================================================================
动机：第 10 章稳定性矩阵显示"快衰减短周期"组 1 日调仓年化 22-28%，
但未计费用——1 日全市场重组换手极大，费后是否为正是关键问题；
同时检验 10 分位（两端更极端）相对 5 分位是否增强。

方法：
- 分层日频回测（与 highfreq_report.layer_backtest 同构）：每 horizon 个
  交易日按因子值 qcut 重组，持有期逐日盯市，层内等权；
- 换手率实算：turn(q,t) = 1 - |new∩old|/|old|，首期建仓 turn=1.0；
- 费用：调仓生效日（t+1）当日层收益扣 turn × 2 × 单边费率（一卖一买）；
- 多空 D10−D1 取各 horizon IC 方向（|IC| 约定，负 IC 取反向）。

因子集：跨期稳定强 4 + 快衰减 3 + 长周期 2（第 10 章矩阵四档代表）。
输出：控制台表格（5 分位一致性自检 / 10 分位增益 / 费用敏感性）。
"""
import sys

sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')

import numpy as np
import pandas as pd

import highfreq_report as hfr  # 模块级已 patch Store 为只读
from highfreq_report import TRADING_DAYS, daily_returns
from quantlab.data.store import Store

# 第 10 章矩阵四档代表因子
FACTORS = [
    "hf_amihud_20", "hf_amtrange_20", "hf_rvvol_20", "hf_dsem_20",      # 跨期稳定强
    "hf_rlast30_20", "hf_smartq_10", "hf_vwapbias_20",                   # 快衰减短周期
    "hf_rsk_20", "hf_corr_rv_20",                                        # 长周期增强
]
HORIZONS = [1, 5, 20]
FEES = [0.0, 0.001, 0.002, 0.003]  # 单边 0 / 10bp / 20bp / 30bp


def bt_with_cost(ret_df, dates_rt, store, name, horizon, n_q, fees):
    """分层日频回测 + 逐期换手扣费。返回 {fee: ls_daily Series} 与稳态换手。"""
    fv = store.read_factor(name)
    if fv is None or fv.empty:
        return None, None
    fv["date"] = pd.to_datetime(fv["date"])
    fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
    fv = fv[np.isfinite(fv["value"])]
    if fv.empty:
        return None, None

    pos = {d: i for i, d in enumerate(dates_rt)}
    fdates = pd.DatetimeIndex(np.sort(fv["date"].unique()))
    rdates = fdates[::horizon]
    by_date = {d: g for d, g in fv.groupby("date")}

    parts = {f: [] for f in fees}
    prev_sets = {}
    turn_records = []

    for t in rdates:
        i0 = pos.get(t)
        if i0 is None or i0 + 1 >= len(dates_rt):
            continue
        seg = dates_rt[i0 + 1: i0 + 1 + horizon]
        cs = by_date.get(t)
        if cs is None or len(cs) < 100:
            continue
        try:
            q = pd.qcut(cs["value"], n_q, labels=False, duplicates="drop")
        except Exception:
            continue
        if q.notna().sum() == 0:
            continue
        qmap = pd.Series(q.to_numpy(), index=cs["code"].to_numpy())
        sub = ret_df.loc[seg[0]:seg[-1]]
        if sub.empty:
            continue
        tmp = pd.DataFrame({"date": sub.index.to_numpy(),
                            "q": sub["code"].map(qmap).to_numpy(),
                            "ret": sub["ret"].to_numpy()}).dropna(subset=["q"])
        if tmp.empty:
            continue
        tmp["q"] = tmp["q"].astype(int)

        # 换手率（逐层）
        cur_sets = {int(qv): set(g["code"]) for qv, g in cs.assign(q=q).dropna(subset=["q"]).groupby("q")}
        turns = {}
        for qv in range(n_q):
            new, old = cur_sets.get(qv, set()), prev_sets.get(qv, set())
            if not new:
                turns[qv] = np.nan
            elif not old:
                turns[qv] = 1.0
            else:
                turns[qv] = 1.0 - len(new & old) / len(old)
        turn_records.append(turns)

        # 各费用情景：生效日（seg 第一天）扣 turn × 2 × fee
        for fee in fees:
            tf = tmp.copy()
            m = tf["date"] == seg[0]
            tf.loc[m, "ret"] = tf.loc[m, "ret"] - tf.loc[m, "q"].map(
                lambda qv: (turns.get(qv, 0.0) or 0.0) * 2 * fee)
            parts[fee].append(
                tf.groupby(["date", "q"])["ret"].mean().unstack().reindex(columns=range(n_q)))

        prev_sets = cur_sets

    if not parts[fees[0]]:
        return None, None

    out = {}
    for fee in fees:
        lr = pd.concat(parts[fee]).sort_index()
        lr = lr[~lr.index.duplicated(keep="first")]
        ls = (lr[n_q - 1] - lr[0]).dropna()
        out[fee] = ls
    tr = pd.DataFrame(turn_records)
    steady = tr.iloc[1:].mean().mean()  # 稳态（除首期建仓）
    return out, steady


def main():
    print("加载日频收益与因子湖…")
    store = Store()
    ret_df = daily_returns().sort_values(["date", "code"]).set_index("date")
    dates_rt = pd.DatetimeIndex(ret_df.index.unique())

    # IC 方向（缓存）
    import pickle
    blob = pickle.load(open("reports/_hf_report_cache.pkl", "rb"))
    ic_dfs = blob["data"][0]
    dirs = {h: {n: (1.0 if float(ic_dfs[h].loc[n, "ic"]) >= 0 else -1.0)
                for n in FACTORS if n in ic_dfs[h].index} for h in HORIZONS}

    results = {}
    for n in FACTORS:
        for h in HORIZONS:
            for n_q in (5, 10):
                out, steady = bt_with_cost(ret_df, dates_rt, store, n, h, n_q, FEES)
                if out is None:
                    continue
                d = dirs[h].get(n, 1.0)
                anns = {}
                for fee in FEES:
                    ls = (out[fee] * d).dropna()
                    anns[fee] = float((1 + ls.mean()) ** TRADING_DAYS - 1)
                results[(n, h, n_q)] = {"anns": anns, "steady_turn": steady}
        print(f"  {n} 完成")

    # ── 表 1：10 分位 vs 5 分位（费前），换手对照 ──
    print("\n=== 表 1：10 分位费前 vs 5 分位费前（有效方向多空年化）===")
    print(f'{"因子":18s} {"h":>3s} {"5分位":>8s} {"10分位":>8s} {"增益":>7s} {"稳态换手":>8s}')
    for n in FACTORS:
        for h in HORIZONS:
            r5, r10 = results.get((n, h, 5)), results.get((n, h, 10))
            if not r5 or not r10:
                continue
            a5, a10 = r5["anns"][0.0], r10["anns"][0.0]
            print(f'{n:18s} {h:3d} {a5:+8.1%} {a10:+8.1%} {a10-a5:+7.1%} {r10["steady_turn"]:8.1%}')

    # ── 表 2：费用敏感性（10 分位）──
    print("\n=== 表 2：10 分位费用敏感性（有效方向多空年化，单边 0/10/20/30bp）===")
    print(f'{"因子":18s} {"h":>3s} {"0bp":>8s} {"10bp":>8s} {"20bp":>8s} {"30bp":>8s} {"费后转负":>8s}')
    for n in FACTORS:
        for h in HORIZONS:
            r10 = results.get((n, h, 10))
            if not r10:
                continue
            a = r10["anns"]
            neg = next((f"{f*1e4:.0f}bp" for f in FEES if a[f] < 0), "-")
            print(f'{n:18s} {h:3d} {a[0.0]:+8.1%} {a[0.001]:+8.1%} {a[0.002]:+8.1%} {a[0.003]:+8.1%} {neg:>8s}')

    import json
    out = {f"{n}|{h}|{n_q}": {"anns": {f"{f*1e4:.0f}bp": v for f, v in r["anns"].items()},
                              "steady_turn": r["steady_turn"]}
           for (n, h, n_q), r in results.items()}
    with open("reports/hf_decile_cost_20260907.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n结果已写入 reports/hf_decile_cost_20260907.json")


if __name__ == "__main__":
    main()
