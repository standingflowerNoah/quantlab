#!/usr/bin/env python3
"""hf_wf_composite 生产因子验证：与 walkforward 研究口径对齐
用法: python scripts/hf_composite_validate.py

验证内容：
  1) 口径一致性：生产复合因子在月度调仓日的截面 IC 应与
     scripts/highfreq_walkforward.py 的事件 IC（方案 B / hf6 / ICIR）
     一致（同训练窗、同权重、同中性化——允许浮点级微小差异）
  2) 日度合成统计：全样本 / 分年 IC（T+5/T+20）、覆盖与方向分布
  3) 观察仓台账：最新截面 Top/Bottom 20 只（人工抽查用）
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


def ic_stats(ic):
    if len(ic) == 0 or ic.std() == 0:
        return {"n": len(ic), "ic": np.nan, "icir": np.nan, "win": np.nan}
    return {"n": len(ic), "ic": round(float(ic.mean()), 4),
            "icir": round(float(ic.mean() / ic.std()), 3),
            "win": round(float((ic > 0).mean()), 3)}


def day_ic(fv, fwd, min_n=30):
    m = fv.merge(fwd, on=["date", "code"], how="inner")
    out = {}
    for d, g in m.groupby("date"):
        if len(g) < min_n:
            continue
        out[d] = g["value"].rank().corr(g["fwd"].rank())
    return pd.Series(out, dtype=float)


def main():
    comp = load_factor("hf_wf_composite")
    print(f"hf_wf_composite: {len(comp):,} 行，"
          f"{comp['date'].min().date()} ~ {comp['date'].max().date()}，"
          f"{comp['code'].nunique()} 只")
    fwd5, fwd20 = fwd_returns(5), fwd_returns(20)

    # 1) 与 walkforward 事件对齐
    ev_path = ROOT / "reports" / "highfreq_walkforward.csv"
    if ev_path.exists():
        ev = pd.read_csv(ev_path, parse_dates=["rebal"])
        ref = (ev[(ev["scheme"] == "B") & (ev["fset"] == "hf6")
                  & (ev["mode"] == "icir")]
               .set_index("rebal"))
        print("\n=== 1) 口径一致性（月度调仓日截面 IC vs 研究事件 IC）===")
        print(f"{'调仓日':12s} {'生产':>8s} {'研究':>8s} {'差异':>8s}")
        diffs = []
        for t, row in ref.iterrows():
            sub = comp[comp["date"] == t]
            if sub.empty:
                # 调仓日无值（如因子起点前）→ 用最近可用日
                nearest = comp[comp["date"] <= t]["date"].max()
                sub = comp[comp["date"] == nearest]
                if sub.empty:
                    continue
            m = sub.merge(fwd20[fwd20["date"] == t], on=["date", "code"])
            if len(m) < 200:
                continue
            ic = m["value"].rank().corr(m["fwd"].rank())
            d = ic - row["ic"]
            diffs.append(d)
            print(f"{t.date()} {ic:+8.4f} {row['ic']:+8.4f} {d:+8.4f}")
        if diffs:
            print(f"平均差异: {np.mean(diffs):+.5f} / 最大: "
                  f"{np.max(np.abs(diffs)):.5f}")
    else:
        print("研究事件 CSV 不存在，跳过对齐")

    # 2) 日度合成统计
    print("\n=== 2) 日度合成统计 ===")
    for tag, fwd in (("T+5", fwd5), ("T+20", fwd20)):
        s = ic_stats(day_ic(comp, fwd))
        print(f"全样本 {tag}: IC {s['ic']:+.4f} / ICIR {s['icir']:+.2f} / "
              f"win {s['win']} / n {s['n']}")
    for yr in (2025, 2026):
        sub = comp[comp["date"].dt.year == yr]
        s5, s20 = ic_stats(day_ic(sub, fwd5)), ic_stats(day_ic(sub, fwd20))
        print(f"{yr}: T+5 {s5['ic']:+.4f}/{s5['icir']:+.2f} | "
              f"T+20 {s20['ic']:+.4f}/{s20['icir']:+.2f}")

    # 3) 观察仓台账：最新截面 Top/Bottom 20
    print("\n=== 3) 最新截面观察仓台账（Top/Bottom 20）===")
    last = comp[comp["date"] == comp["date"].max()].sort_values("value")
    print(f"截面日: {comp['date'].max().date()}，{len(last)} 只")
    print("Bottom 20（预期低收益）:",
          ", ".join(last.head(20)["code"].tolist()))
    print("Top 20（预期高收益）:",
          ", ".join(last.tail(20)["code"].tolist()))

    print("\n完成。")


if __name__ == "__main__":
    main()
