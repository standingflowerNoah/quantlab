"""观察池因子 size 分域实验（B1 研报口径：股东户数因子中小市值有效、大盘失效）
======================================================================
对象：holder_num_chg_2（观察池：IC20 −0.028 五年全负但组合层不稳定）、
      ev_high_vol_20（顺带：负向事件簇的大小盘强度差）
设计：size 三分域（小/中/大）域内 pct-rank，每 5 日截面采样，
      分域 vs 全市场 IC 对比 + 分年度 delta 一致性判定。
域宽参照研报：小盘 ≈ 30 分位以下，中盘 30~70，大盘 70 以上（qcut 33/67 更简，
用 qcut(3) 与研报 62~113 亿口径方向一致即可，精确分界由数据说话）。
"""
import sys
sys.path.insert(0, '.')

import numpy as np
import pandas as pd

from quantlab.data.store import Store, query

STORE = Store()


def load_factor(name: str) -> pd.DataFrame:
    fv = STORE.read_factor(name)
    fv["date"] = pd.to_datetime(fv["date"])
    fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
    return fv.dropna(subset=["value"])[["date", "code", "value"]]


def load_fwd(horizon: int) -> pd.DataFrame:
    df = query(f"""
        SELECT date, code, c_lead / c - 1 AS fwd
        FROM (
            SELECT date, code, close * adj_factor AS c,
                   LEAD(close * adj_factor, {horizon}) OVER (
                       PARTITION BY code ORDER BY date) AS c_lead
            FROM kline_daily
        )
        WHERE c_lead IS NOT NULL AND c > 0
    """)
    df["date"] = pd.to_datetime(df["date"])
    return df


def domain_ic(fv: pd.DataFrame, size: pd.DataFrame | None,
              fwd: pd.DataFrame, sample_every: int = 5) -> pd.DataFrame:
    """size=None 全市场对照；否则按 size 三分域域内 rank"""
    m = fv.merge(fwd, on=["date", "code"], how="inner")
    if size is not None:
        m = m.merge(size.rename(columns={"value": "size"}), on=["date", "code"],
                    how="inner").dropna(subset=["size"])
        m["dom"] = pd.qcut(m["size"].rank(method="first"), q=3,
                           labels=["small", "mid", "large"])
        m["r"] = m.groupby(["date", "dom"], observed=True)["value"].rank(pct=True)
    else:
        m["r"] = m.groupby("date")["value"].rank(pct=True)
    dates = np.sort(m["date"].unique())[::sample_every]
    m = m[m["date"].isin(set(dates))]
    g = m.groupby("date").apply(
        lambda x: pd.Series({"ic": x["r"].corr(x["fwd"].rank()),
                             "n": len(x)}), include_groups=False)
    return g[g["n"] >= 100].reset_index()


def yearly_ic(ic_series: pd.Series, dates: pd.Series) -> dict:
    y = pd.DatetimeIndex(dates).year
    return {int(k): round(float(v), 4)
            for k, v in ic_series.groupby(y).mean().items()}


def main():
    size = load_factor("size")
    fwd5, fwd20 = load_fwd(5), load_fwd(20)

    for fname, fwd, h in [("holder_num_chg_2", fwd20, 20),
                          ("holder_num_chg_2", fwd5, 5),
                          ("ev_high_vol_20", fwd20, 20)]:
        fv = load_factor(fname)
        raw = domain_ic(fv, None, fwd)
        dom = domain_ic(fv, size, fwd)
        print(f"\n{'='*78}\n{fname} × size 三分域（h{h}）\n{'='*78}")
        print(f"全市场: IC={raw['ic'].mean():+.4f} "
              f"ICIR={raw['ic'].mean()/raw['ic'].std():+.3f} n={len(raw)}")
        print(f"分域整体: IC={dom['ic'].mean():+.4f} "
              f"ICIR={dom['ic'].mean()/dom['ic'].std():+.3f} n={len(dom)}")
        # 分域各自 IC（合并域内 rank 无法拆域——重算每域独立 IC）
        m = fv.merge(fwd, on=["date", "code"], how="inner").merge(
            size.rename(columns={"value": "size"}), on=["date", "code"],
            how="inner").dropna(subset=["size"])
        m["dom"] = pd.qcut(m["size"].rank(method="first"), q=3,
                           labels=["small", "mid", "large"])
        dates = np.sort(m["date"].unique())[::5]
        m = m[m["date"].isin(set(dates))]
        for dname in ("small", "mid", "large"):
            sub = m[m["dom"] == dname]
            ics = sub.groupby("date").apply(
                lambda x: x["value"].rank().corr(x["fwd"].rank()),
                include_groups=False)
            ics = ics.dropna()
            print(f"  {dname:5s}: IC={ics.mean():+.4f} "
                  f"ICIR={ics.mean()/ics.std():+.3f} "
                  f"分年={yearly_ic(ics, ics.index)}")


if __name__ == "__main__":
    main()
