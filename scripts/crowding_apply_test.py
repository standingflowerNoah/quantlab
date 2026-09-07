"""因子拥挤度 / 估值指标 → 应用潜力实证（batch14）
====================================================================
问题：quantlab/factor/crowding.py 产出的三类指标（综合拥挤度 crowding、
估值价差 z val_z、配对相关 z corr_z、换手分位 turnover_pct）对因子
未来多空收益是否有预测力？可否用于择时/轮动/预警？

数据（全部湖直读，不触碰 DuckDB 写锁）：
- 指标：data/lake/crowding/history.parquet（5 交易日采样，因果滚动归一）
- 因子值：data/lake/factor/<name>/part-*.parquet
- 收益：data/lake/clean/mirror/kline_daily.parquet（close*adj_factor）
- 股票池近似：instruments.parquet 剔 ST/北交所/上市<120日（is_st 为当前
  快照，ST 时变性未建模——研究级近似，多空两端影响有限）

检验：
A. 择时 IC：indicator_t vs 因子多空未来 5/20/60 日收益的 rank 相关
   （重叠 + 非重叠两条口径，非重叠给 t 值）
B. 事件研究：拥挤分位≥80% 事件后 20 日收益 vs 无条件均值；
   val_z 极端便宜(<-1)/贵(>1) 分组
C. 轮动回测：20 日非重叠块，按 val_z 最便宜/最不拥挤 选 top2 因子
   等权持有多空，对比全因子等权与 size+amihud 静态基线

输出：reports/crowding_timing_20260907.json
"""
from __future__ import annotations

import sys
sys.path.insert(0, ".")

import json
import os
import time
import warnings
from pathlib import Path
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from quantlab.model.composite import FACTOR_DIRECTION

HIST = "data/lake/crowding/history.parquet"
FACTORS = ["size", "amihud_20", "momentum_20", "reversal_5", "volatility_20",
           "price_position_250", "overnight_mom_20", "chip_vwap_bias_250",
           "sue", "sue_i"]
INDICATORS = ["crowding", "val_z", "corr_z", "turnover_pct"]
FWD = [5, 20, 60]
LONG_MIN = "2022-07-01"      # 与门控/回测一致的研究起点


def tstat(r: float, n: int) -> float:
    if n < 5 or abs(r) >= 1:
        return np.nan
    return r * np.sqrt(n - 2) / np.sqrt(1 - r * r)


def rank_ic(a: pd.Series, b: pd.Series) -> tuple[float, int]:
    m = a.notna() & b.notna()
    n = int(m.sum())
    if n < 10:
        return np.nan, n
    return float(a[m].rank().corr(b[m].rank())), n


def main():
    t0 = time.time()
    # ── 收益矩阵 ────────────────────────────────────────────────
    import duckdb
    con = duckdb.connect()
    px = con.execute(
        "SELECT date, code, close*adj_factor AS c "
        "FROM 'data/lake/clean/mirror/kline_daily.parquet'").df()
    px["date"] = pd.to_datetime(px["date"])
    pmat = px.pivot(index="date", columns="code", values="c").sort_index()
    rmat = pmat.pct_change()
    td = rmat.index

    ins = con.execute(
        "SELECT code, is_st, board "
        "FROM 'data/lake/clean/mirror/instruments.parquet'").df()
    uni = ins[(ins["is_st"] != True) & (ins["board"] != "BJ")]["code"]  # noqa: E712
    # 次新代理：收益矩阵首个有效日（instruments.list_date 全空，用可得数据近似
    # ashare_ex 的上市<120 交易日剔除）。首 10 个交易日内已有收益 = 老股票
    # （fvi <= td[10] 恒过），否则须满足 fvi <= td[i-120]（120 日历史）
    fvi = rmat.notna().idxmax()
    fvi = fvi[fvi > td[0]]

    hist = pd.read_parquet(HIST)
    hist = hist[hist["factor"].isin(FACTORS)].copy()
    hist["date"] = pd.to_datetime(hist["date"])

    # ── A. 面板：每因子每采样日的多空前向收益（带本地缓存） ─────────
    cache = Path("data/lake/crowding/ls_panel.parquet")
    if cache.exists() and not os.environ.get("CT_REBUILD"):
        panel = pd.read_parquet(cache)
        panel["date"] = pd.to_datetime(panel["date"])
        print(f"面板缓存载入 {len(panel)} 行", flush=True)
    else:
        _build_rows = []
        for name in FACTORS:
            d = int(FACTOR_DIRECTION.get(name, 1))
            fv = con.execute(
                f"SELECT date, code, value FROM 'data/lake/factor/{name}/part-*.parquet'"
            ).df()
            fv["date"] = pd.to_datetime(fv["date"])
            fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
            fv = fv[np.isfinite(fv["value"])]
            samp = sorted(set(hist.loc[hist["factor"] == name, "date"])
                          & set(fv["date"]))
            pos = {dt: i for i, dt in enumerate(td)}
            for dt in samp:
                if dt not in pos:
                    continue
                i = pos[dt]
                if i + max(FWD) + 1 >= len(td):
                    continue
                g = fv[fv["date"] == dt]
                g = g[g["code"].isin(uni)]
                cutoff = td[max(i - 120, 10)]
                g = g[g["code"].isin(fvi[fvi <= cutoff].index)]
                g = g.set_index("code")
                n = len(g)
                if n < 200:
                    continue
                r = g["value"].rank(pct=True) * d
                k = max(int(n * 0.2), 20)
                lng = r.nlargest(k).index[:100]
                sh = r.nsmallest(k).index[:100]
                fwd = {}
                for h in FWD:
                    seg = rmat.iloc[i + 1: i + 1 + h]
                    fwd[h] = float(seg[lng].mean(axis=1).sum()
                                   - seg[sh].mean(axis=1).sum())
                _build_rows.append({"factor": name, "date": dt, "n": n,
                                    **{f"fwd{h}": fwd[h] for h in FWD}})
            print(f"  {name}: {len(_build_rows)} 累计行", flush=True)
        panel = pd.DataFrame(_build_rows)
        panel.to_parquet(cache, index=False)
        print(f"面板 {len(panel)} 行，已缓存 → {cache}", flush=True)
    panel = hist.merge(panel, on=["factor", "date"], how="inner")
    print(f"面板 {len(panel)} 行 "
          f"({panel['factor'].nunique()} 因子 × {panel['date'].nunique()} 日)，"
          f"{time.time()-t0:.0f}s", flush=True)

    # 每因子拥挤度因果滚动分位（与 monitor 口径一致：100 点窗 min40）
    pan = []
    for name, g in panel.sort_values("date").groupby("factor"):
        g = g.copy()
        c = g["crowding"]
        g["crowd_pct"] = c.rolling(100, min_periods=40).apply(
            lambda a: float(np.mean(a <= a[-1])), raw=True)
        pan.append(g)
    panel = pd.concat(pan)

    # ── A. 择时 IC ──────────────────────────────────────────────
    out_a = {}
    for ind in INDICATORS + ["crowd_pct"]:
        per_factor = {}
        for name, g in panel.groupby("factor"):
            e = {}
            for h in FWD:
                ic, n = rank_ic(g[ind], g[f"fwd{h}"])
                # 非重叠（每 4 个采样点 ≈ 20 交易日）口径
                g2 = g.iloc[::4]
                ic4, n4 = rank_ic(g2[ind], g2[f"fwd{h}"])
                e[f"h{h}"] = {"ic": round(ic, 3) if np.isfinite(ic) else None,
                              "n": n,
                              "ic_nolap": round(ic4, 3) if np.isfinite(ic4) else None,
                              "t_nolap": round(tstat(ic4, n4), 2)
                              if np.isfinite(ic4) else None}
            per_factor[name] = e
        # 池化：指标按因子内 z 标准化
        z = panel.groupby("factor")[ind].transform(
            lambda s: (s - s.mean()) / (s.std() or np.nan))
        pooled = {}
        for h in FWD:
            ic, n = rank_ic(z, panel[f"fwd{h}"])
            zi = z.iloc[::4].reset_index(drop=True)
            yi = panel[f"fwd{h}"].iloc[::4].reset_index(drop=True)
            ic4, n4 = rank_ic(zi, yi)
            pooled[f"h{h}"] = {"ic": round(ic, 3), "n": n,
                               "ic_nolap": round(ic4, 3), "n_nolap": n4,
                               "t_nolap": round(tstat(ic4, n4), 2)}
        out_a[ind] = {"pooled": pooled, "per_factor": per_factor}

    # ── B. 事件研究（20 日前向） ────────────────────────────────
    f20 = panel["fwd20"]
    uncond = float(f20.mean())
    hi = panel[panel["crowd_pct"] >= 0.8]["fwd20"]
    lo = panel[panel["crowd_pct"] <= 0.2]["fwd20"]
    vz_cheap = panel[panel["val_z"] <= -1]["fwd20"]
    vz_exp = panel[panel["val_z"] >= 1]["fwd20"]
    out_b = {
        "unconditional_20d": round(uncond, 4),
        "crowd_ge80": {"mean": round(float(hi.mean()), 4), "n": len(hi)},
        "crowd_le20": {"mean": round(float(lo.mean()), 4), "n": len(lo)},
        "val_cheap_le-1": {"mean": round(float(vz_cheap.mean()), 4), "n": len(vz_cheap)},
        "val_expensive_ge1": {"mean": round(float(vz_exp.mean()), 4), "n": len(vz_exp)},
    }

    # ── C. 轮动回测（20 日非重叠块） ────────────────────────────
    long_names = [n for n in FACTORS
                  if panel[panel["factor"] == n]["date"].min()
                  <= pd.Timestamp(LONG_MIN)]
    blocks = []
    dates_all = sorted(panel["date"].unique())
    d0 = [d for d in dates_all if d >= pd.Timestamp(LONG_MIN)]
    starts = d0[::4]
    blk = panel.set_index("date")
    for s in starts:
        rows_b = {"date": s}
        valid = [n for n in long_names if s in blk.loc[blk["factor"] == n].index]
        for n in valid:
            rec = blk.loc[blk["factor"] == n].loc[s]
            rows_b[n] = rec["fwd20"]
            rows_b[f"{n}__val"] = rec["val_z"]
            rows_b[f"{n}__crd"] = rec["crowding"]
        blocks.append(rows_b)
    B = pd.DataFrame(blocks).set_index("date")
    ret_cols = {n: [c for c in B.columns if c == n] for n in long_names}

    def port(sel_fn):
        rets, picks = [], []
        for dt, row in B.iterrows():
            valid = [n for n in long_names if pd.notna(row.get(n))]
            if len(valid) < 3:
                continue
            pick = sel_fn(row, valid)
            rets.append(np.mean([row[n] for n in pick]))
            picks.append(pick)
        return pd.Series(rets, index=B.index[:len(rets)]), picks

    ew = port(lambda r, v: v)
    cheap = port(lambda r, v: sorted(v, key=lambda n: r.get(f"{n}__val") if pd.notna(r.get(f"{n}__val")) else 9)[:2])
    quiet = port(lambda r, v: sorted(v, key=lambda n: r.get(f"{n}__crd") if pd.notna(r.get(f"{n}__crd")) else 9)[:2])
    core = port(lambda r, v: [n for n in ["size", "amihud_20"] if n in v])

    def ann(s):
        per = 20 / 252
        m = s.mean()
        return {"annual": round(float(m / per), 4),
                "sharpe": round(float(m / (s.std() + 1e-12) / np.sqrt(per)), 2),
                "n_blocks": len(s)}

    out_c = {"all_ew": ann(ew[0]), "top2_by_val_cheap": ann(cheap[0]),
             "top2_by_crowding_low": ann(quiet[0]),
             "static_core_size_amihud": ann(core[0]),
             "picks_val_cheap_tail": {str(dt): p for dt, p in
                                      zip(cheap[0].index[-6:], cheap[1][-6:])}}

    result = {"config": {"fwd": FWD, "quantile": 0.2, "long_start": LONG_MIN,
                         "long_factors": long_names},
              "A_timing_ic": out_a, "B_event": out_b, "C_rotation": out_c}
    with open("reports/crowding_timing_20260907.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1, default=str)

    # ── 控制台摘要 ──────────────────────────────────────────────
    print("\n=== A. 池化择时 IC（指标→未来多空收益）===")
    for ind, d in out_a.items():
        p = d["pooled"]
        f = lambda x: f"{x['ic']:+.3f}" if x["ic"] is not None else "  n/a"
        ft = lambda x: f"(t={x['t_nolap']:+.2f})" if x.get("t_nolap") is not None else ""
        print(f"  {ind:12s} h5 {f(p['h5'])}  h20 {f(p['h20'])} {ft(p['h20'])}  h60 {f(p['h60'])}")
    print("\n  分因子 h20 IC（非重叠口径）:")
    hdr = f"  {'factor':22s}" + "".join(f"{i:>14s}" for i in INDICATORS)
    print(hdr)
    for name in long_names:
        line = f"  {name:22s}"
        for ind in INDICATORS:
            e = out_a[ind]["per_factor"][name]["h20"]
            line += f"  {e['ic_nolap']:+.3f}{e['t_nolap']:+5.1f}" if e["ic_nolap"] is not None else "        n/a"
        print(line)
    print("\n=== B. 事件研究（20 日前向）===")
    for k, v in out_b.items():
        print(f"  {k}: {v}")
    print("\n=== C. 轮动回测 ===")
    for k, v in out_c.items():
        if isinstance(v, dict) and "annual" in v:
            print(f"  {k}: 年化 {v['annual']:+.1%} 夏普 {v['sharpe']} ({v['n_blocks']} 块)")
    print(f"\n完成 {time.time()-t0:.0f}s → reports/crowding_timing_20260907.json")


if __name__ == "__main__":
    main()
