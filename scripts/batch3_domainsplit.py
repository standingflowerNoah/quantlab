"""C1 因子分域实验（第三批 · 光大量化选股系列十四/十五思路）
=====================================
同一因子在不同域内差异化计算（域内截面排名），对比分域前后 IC：

实验 1：reversal_5 × ROE 同比三分域
    研报口径：超跌但基本面未坏（高 ROE 域）反转更有效，周频 RankIC 3.77%→8.03%
    ROE 同比 = roe − roe_{-252 交易日}（roe 为 finance_history 多期版日频序列）
实验 2：ep × size 二分域（估值在小市值域更稳健）
实验 3：bp × size 二分域

方法：
- 每 5 个交易日采样一个截面，horizon=5（反转）/20（估值）
- 域内 rank：groupby(date, dom) pct rank（0~1），域数 ≤3 防过拟合
- 对照组：同一批截面上原始因子全市场 rank 的 IC
- 判定：分域 IC 增量需分年度一致，单一窗口翻正不算数
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


def roe_yoy() -> pd.DataFrame:
    """roe 因子日频序列的 252 交易日差分 → ROE 同比"""
    roe = load_factor("roe").sort_values(["code", "date"])
    roe["roe_yoy"] = roe.groupby("code")["value"].diff(252)
    return roe.dropna(subset=["roe_yoy"])[["date", "code", "roe_yoy"]]


def domain_ic(fv: pd.DataFrame, dom: pd.DataFrame | None,
              fwd: pd.DataFrame, horizon: int, sample_every: int = 5,
              dom_names=(0, 1, 2)) -> pd.DataFrame:
    """分域（dom=None 为全市场对照）逐截面 Rank IC 序列"""
    m = fv.merge(fwd, on=["date", "code"], how="inner")
    if dom is not None:
        dom = dom.rename(columns={dom.columns[-1]: "domvar"})
        m = m.merge(dom, on=["date", "code"], how="inner")
        m = m.dropna(subset=["domvar"])
        m["dom"] = pd.qcut(m["domvar"].rank(method="first"),
                           q=len(dom_names), labels=dom_names)
        m["r"] = m.groupby(["date", "dom"], observed=True)["value"].rank(pct=True)
    else:
        m["r"] = m.groupby("date")["value"].rank(pct=True)
    m = m[m["date"].dt.dayofweek.isin([0, 1, 2, 3, 4])]
    dates = np.sort(m["date"].unique())[::sample_every]
    m = m[m["date"].isin(set(dates))]
    g = m.groupby("date").apply(
        lambda x: pd.Series({"ic": x["r"].corr(x["fwd"].rank()),
                             "n": len(x)}), include_groups=False)
    return g[g["n"] >= 100].reset_index()


def report(title: str, raw: pd.DataFrame, dom: pd.DataFrame):
    raw["year"] = raw["date"].dt.year
    dom["year"] = dom["date"].dt.year
    print(f"\n{'='*78}\n{title}\n{'='*78}")
    overall = pd.DataFrame({
        "全市场": {"ic": raw["ic"].mean(),
                "icir": raw["ic"].mean() / raw["ic"].std(),
                "n_days": len(raw)},
        "分域": {"ic": dom["ic"].mean(),
               "icir": dom["ic"].mean() / dom["ic"].std(),
               "n_days": len(dom)},
    }).T.round(4)
    print(overall.to_string())
    yearly = pd.DataFrame({
        "raw": raw.groupby("year")["ic"].mean().round(4),
        "dom": dom.groupby("year")["ic"].mean().round(4),
    })
    yearly["delta"] = (yearly["dom"] - yearly["raw"]).round(4)
    print("\n分年度（分域增益是否逐年一致）:")
    print(yearly.to_string())


def main():
    fwd5, fwd20 = load_fwd(5), load_fwd(20)
    ry = roe_yoy()

    # ── 实验 1：reversal_5 × ROE 同比三分域 ──
    rev = load_factor("reversal_5")
    report("实验1  reversal_5 × ROE同比三分域（horizon=5）",
           domain_ic(rev, None, fwd5, 5),
           domain_ic(rev, ry, fwd5, 5))

    # ── 实验 2：ep × size 二分域 ──
    ep, size = load_factor("ep"), load_factor("size")
    report("实验2  ep × size 二分域（horizon=20）",
           domain_ic(ep, None, fwd20, 20, dom_names=(0, 1)),
           domain_ic(ep, size, fwd20, 20, dom_names=(0, 1)))

    # ── 实验 3：bp × size 二分域 ──
    bp = load_factor("bp")
    report("实验3  bp × size 二分域（horizon=20）",
           domain_ic(bp, None, fwd20, 20, dom_names=(0, 1)),
           domain_ic(bp, size, fwd20, 20, dom_names=(0, 1)))


if __name__ == "__main__":
    main()
