#!/usr/bin/env python3
"""两族 walk-forward 合成因子与生产核心的互补性检验
用法: python scripts/composite_complementarity.py

回答三个问题：
  1) hf_wf_composite 与 a191_wf_composite 的截面相关——两条研究主线
     （分钟微观结构 vs 日频价量）是否携带正交信息？
  2) a191_wf_composite 与生产核心因子（size/amihud_20/dragon_net_20）
     的相关——接入生产是否双重计费（并行会话的担忧）？
  3) 联合合成（等权 zscore）的 IC——分散化是否有增量？

纯 Parquet 路径。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import duckdb

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake"
_con = duckdb.connect()


def q(sql: str) -> pd.DataFrame:
    return _con.execute(sql).df()


def _p(sub: str) -> str:
    return str(LAKE / sub).replace("\\", "/")


def zscore_s(g):
    s = g.std()
    return (g - g.mean()) / s if s and np.isfinite(s) else g * np.nan


def load_factor(name):
    g = _p(f"factor/{name}") + "/part-*.parquet"
    fv = q(f"SELECT date, code, value FROM read_parquet('{g}')")
    fv["date"] = pd.to_datetime(fv["date"])
    fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
    return fv.dropna(subset=["value"])


def fwd_returns(horizon):
    kl = _p("clean/mirror/kline_daily.parquet")
    df = q(f"""
        SELECT date, code, c_lead / c - 1 AS fwd FROM (
            SELECT date, code, close * adj_factor AS c,
                   LEAD(close * adj_factor, {horizon}) OVER (
                       PARTITION BY code ORDER BY date) AS c_lead
            FROM read_parquet('{kl}'))
        WHERE c_lead IS NOT NULL AND c > 0
    """)
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "code", "fwd"]].dropna()


def day_ic(fv, fwd, min_n=30):
    m = fv.merge(fwd, on=["date", "code"], how="inner")
    out = {}
    for d, g in m.groupby("date"):
        if len(g) < min_n:
            continue
        out[d] = g["value"].rank().corr(g["fwd"].rank())
    return pd.Series(out, dtype=float)


def ic_stats(ic):
    if len(ic) == 0 or ic.std() == 0:
        return {"n": len(ic), "ic": np.nan, "icir": np.nan, "win": np.nan}
    return {"n": len(ic), "ic": round(float(ic.mean()), 4),
            "icir": round(float(ic.mean() / ic.std()), 3),
            "win": round(float((ic > 0).mean()), 3)}


def xsec_corr(a, b):
    """两因子逐日截面秩相关的均值（方向已统一的值直接比）"""
    m = a.merge(b, on=["date", "code"], how="inner",
                suffixes=("_a", "_b"))
    out = {}
    for d, g in m.groupby("date"):
        if len(g) < 100:
            continue
        out[d] = g["value_a"].rank().corr(g["value_b"].rank())
    return pd.Series(out, dtype=float)


def combine(factors: dict, fwd, tag):
    """等权 zscore 合成 + IC 统计"""
    base = None
    for n, fv in factors.items():
        v = fv.assign(z=fv.groupby("date")["value"].transform(zscore_s)
                      )[["date", "code", "z"]].rename(columns={"z": n})
        base = v if base is None else base.merge(
            v, on=["date", "code"], how="inner")
    cols = [n for n in factors if n in base.columns]
    base["value"] = base[cols].mean(axis=1)
    comp = base[["date", "code", "value"]].dropna()
    s5, s20 = ic_stats(day_ic(comp, fwd[0])), ic_stats(day_ic(comp, fwd[1]))
    print(f"{tag:28s} T+5 {s5['ic']:+.4f}/{s5['icir']:+.2f} | "
          f"T+20 {s20['ic']:+.4f}/{s20['icir']:+.2f} (n={s20['n']})")
    return comp


def main():
    hf = load_factor("hf_wf_composite")
    a191 = load_factor("a191_wf_composite")
    size = load_factor("size")
    amihud = load_factor("amihud_20")
    dragon = load_factor("dragon_net_20")
    fwd5, fwd20 = fwd_returns(5), fwd_returns(20)

    print(f"hf_wf_composite:    {hf['date'].min().date()} ~ "
          f"{hf['date'].max().date()}（{len(hf):,} 行）")
    print(f"a191_wf_composite:  {a191['date'].min().date()} ~ "
          f"{a191['date'].max().date()}（{len(a191):,} 行）")

    # 1) hf ~ a191（重叠期 2025-04 起）
    print("\n=== 1) hf ~ a191 截面秩相关（重叠期）===")
    c = xsec_corr(hf, a191)
    print(f"均值 {c.mean():+.3f} / 中位 {c.median():+.3f} / "
          f"日均覆盖 {len(c)} 日（{c.index.min().date()}~{c.index.max().date()}）")
    for yr in sorted(pd.DatetimeIndex(c.index).year.unique()):
        sub = c[pd.DatetimeIndex(c.index).year == yr]
        print(f"  {yr}: {sub.mean():+.3f}")

    # 2) a191 ~ 生产核心（全历史）
    print("\n=== 2) a191 ~ 生产核心因子截面秩相关 ===")
    for name, fv, direction in (("size", size, -1), ("amihud_20", amihud, +1),
                                ("dragon_net_20", dragon, -1)):
        d = fv.assign(value=fv["value"] * direction)   # 方向统一（预测力同向）
        cc = xsec_corr(a191, d)
        print(f"  a191 ~ {name:15s}: {cc.mean():+.3f}（{len(cc)} 日）")

    # hf ~ 生产核心（重叠期，参考）
    print("\n=== hf ~ 生产核心因子（重叠期，参考）===")
    for name, fv, direction in (("size", size, -1), ("amihud_20", amihud, +1)):
        d = fv.assign(value=fv["value"] * direction)
        cc = xsec_corr(hf, d)
        print(f"  hf   ~ {name:15s}: {cc.mean():+.3f}（{len(cc)} 日）")

    # 3) 联合合成
    print("\n=== 3) 联合合成 IC（等权 zscore）===")
    print(f"{'组合':28s} {'T+5':>18s} {'T+20':>18s}")
    combine({"a191": a191}, (fwd5, fwd20), "a191 单独（全历史）")
    combine({"hf": hf}, (fwd5, fwd20), "hf 单独（重叠期起点 2025-04）")
    ov = (a191[a191["date"] >= hf["date"].min()])
    combine({"a191": ov, "hf": hf}, (fwd5, fwd20), "a191+hf 等权（重叠期）")
    combine({"a191": ov, "size": size.assign(value=-size["value"]),
             "amihud": amihud}, (fwd5, fwd20), "a191+size+amihud（重叠期）")
    # 全历史：a191 与生产核心二元/三元组合
    combine({"a191": a191, "size": size.assign(value=-size["value"])},
            (fwd5, fwd20), "a191+size（全历史）")
    combine({"a191": a191, "amihud": amihud}, (fwd5, fwd20), "a191+amihud（全历史）")
    combine({"a191": a191, "size": size.assign(value=-size["value"]),
             "amihud": amihud}, (fwd5, fwd20), "a191+size+amihud（全历史）")

    # 分年：a191+size+amihud vs 纯 a191（生产核心是否稀释 a191 的失效年）
    print("\n=== 分年 T+20 ICIR：a191 单独 vs a191+size+amihud ===")
    tri = combine({"a191": a191, "size": size.assign(value=-size["value"]),
                   "amihud": amihud}, (fwd5, fwd20), "")
    for yr in (2022, 2023, 2024, 2025, 2026):
        a = ic_stats(day_ic(a191[a191["date"].dt.year == yr], fwd20))
        t = ic_stats(day_ic(tri[tri["date"].dt.year == yr], fwd20))
        print(f"  {yr}: a191 {a['ic']:+.4f}/{a['icir']:+.2f} | "
              f"三因子 {t['ic']:+.4f}/{t['icir']:+.2f}")

    print("\n完成。")


if __name__ == "__main__":
    main()
