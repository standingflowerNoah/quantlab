#!/usr/bin/env python3
"""a191_wf_composite 生产因子验证
用法: python scripts/a191_composite_validate.py

验证内容：
  1) 与研究事件对齐：生产合成（9 因子去重集）在月度调仓日的截面 IC
     vs scripts/alpha191_deep.py 的事件 IC（10 因子集）——预期接近
     但非完全一致（alpha148 剔除），量化去重效应
  2) 精确一致性抽查：3 个抽样调仓日用相同 9 因子集 + 静态 alpha 湖
     重算合成，与生产值做 Spearman（应 ≈1.0，检验公式重算 == 湖数据）
  3) 日度合成统计：全样本/分年 IC（T+5/T+20）
  4) 观察仓台账：最新截面 Top/Bottom 20
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

A191 = ["alpha013", "alpha015", "alpha016", "alpha026", "alpha055",
        "alpha116", "alpha119", "alpha126", "alpha183"]
STYLES = ["size", "turnover", "volatility_20"]
TRAIN_MONTHS, PURGE_TD, MIN_IC_DAYS = 12, 21, 40


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


def main():
    comp = load_factor("a191_wf_composite")
    print(f"a191_wf_composite: {len(comp):,} 行，"
          f"{comp['date'].min().date()} ~ {comp['date'].max().date()}，"
          f"{comp['code'].nunique()} 只")
    fwd5, fwd20 = fwd_returns(5), fwd_returns(20)

    # 1) 与研究事件对齐（10 因子集 vs 生产 9 因子集）
    ev_path = ROOT / "reports" / "alpha191_walkforward.csv"
    if ev_path.exists():
        ev = pd.read_csv(ev_path, parse_dates=["rebal"])
        ref = ev[ev["mode"] == "icir"].set_index("rebal")
        print("\n=== 1) 对齐：生产(9因子) vs 研究(10因子) 月度事件 IC ===")
        diffs = []
        for t, row in ref.iterrows():
            sub = comp[comp["date"] == t]
            if sub.empty:
                continue
            m = sub.merge(fwd20[fwd20["date"] == t], on=["date", "code"])
            if len(m) < 200:
                continue
            ic = m["value"].rank().corr(m["fwd"].rank())
            d = ic - row["ic"]
            diffs.append(d)
        if diffs:
            print(f"对齐事件 {len(diffs)} 个：平均差异 {np.mean(diffs):+.5f} / "
                  f"最大 |差异| {np.max(np.abs(diffs)):.5f}")
        # 抽样打印前后各 3 个
        for t, row in ref.head(3).iterrows():
            sub = comp[comp["date"] == t]
            m = sub.merge(fwd20[fwd20["date"] == t], on=["date", "code"])
            if len(m) >= 200:
                ic = m["value"].rank().corr(m["fwd"].rank())
                print(f"  {t.date()}: 生产 {ic:+.4f} vs 研究 {row['ic']:+.4f}")
        for t, row in ref.tail(3).iterrows():
            sub = comp[comp["date"] == t]
            m = sub.merge(fwd20[fwd20["date"] == t], on=["date", "code"])
            if len(m) >= 200:
                ic = m["value"].rank().corr(m["fwd"].rank())
                print(f"  {t.date()}: 生产 {ic:+.4f} vs 研究 {row['ic']:+.4f}")

    # 2) 精确一致性抽查：3 个抽样日用静态 alpha 湖 + 9 因子集重算
    print("\n=== 2) 精确一致性抽查（9 因子集，静态湖 vs 生产重算）===")
    st = load_factor(STYLES[0]).rename(columns={"value": STYLES[0]})
    for c in STYLES[1:]:
        st = st.merge(load_factor(c).rename(columns={"value": c}),
                      on=["date", "code"], how="outer")
    all_days = sorted(fwd20["date"].unique())
    day_idx = {d: i for i, d in enumerate(all_days)}
    # 抽样日：早期/中期/近期各一（须在 alpha 湖覆盖内且有研究事件）
    sample_ts = [pd.Timestamp("2023-06-01"), pd.Timestamp("2025-03-03"),
                 pd.Timestamp("2026-07-01")]
    pure = {}
    for n in A191:
        fv = load_factor(n)
        df = fv.merge(st, on=["date", "code"], how="left")
        for c in STYLES:
            df[c] = df.groupby("date")[c].transform(
                lambda x: x.rank(pct=True) - 0.5)
        df = df.dropna(subset=STYLES + ["value"])
        parts = []
        for d, g in df.groupby("date"):
            if len(g) < 100:
                continue
            X = np.column_stack([np.ones(len(g))] +
                                [g[c].values for c in STYLES])
            y = g["value"].values.astype(float)
            beta = np.linalg.lstsq(X, y, rcond=None)[0]
            parts.append(g[["date", "code"]].assign(value=y - X @ beta))
        pure[n] = pd.concat(parts, ignore_index=True)
    for t in sample_ts:
        if t not in day_idx or day_idx[t] < PURGE_TD:
            print(f"  {t.date()}: 跳过（不在日历）")
            continue
        purge_end = all_days[day_idx[t] - PURGE_TD]
        w_start = purge_end - pd.DateOffset(months=TRAIN_MONTHS)
        base, wsum = None, 0.0
        for n in A191:
            p = pure[n]
            m = p.merge(fwd20, on=["date", "code"], how="inner")
            ic_s = {}
            for d, g in m.groupby("date"):
                if len(g) >= 30:
                    ic_s[d] = g["value"].rank().corr(g["fwd"].rank())
            w = pd.Series(ic_s, dtype=float)
            w = w[(w.index >= w_start) & (w.index <= purge_end)]
            if len(w) < MIN_IC_DAYS:
                continue
            d_, w_ = np.sign(w.mean()), max(w.mean() / w.std(), 0.0)
            if d_ == 0 or w_ == 0:
                continue
            v = p[p["date"] == t]
            v = v.assign(z=v.groupby("date")["value"].transform(zscore_s)
                         * d_)[["date", "code", "z"]].rename(columns={"z": n})
            base = v.assign(**{n: v[n] * w_}) if base is None else \
                base.merge(v, on=["date", "code"], how="inner")
            wsum += w_
        if base is None or wsum == 0:
            print(f"  {t.date()}: 无有效因子")
            continue
        cols = [n for n in A191 if n in base.columns]
        base["re"] = base[cols].sum(axis=1) / wsum
        prod = comp[comp["date"] == t][["date", "code", "value"]]
        mm = base.merge(prod, on=["date", "code"])
        rho = mm["re"].rank().corr(mm["value"].rank())
        print(f"  {t.date()}: 重算 vs 生产 Spearman = {rho:.4f}（n={len(mm)}）")

    # 3) 日度合成统计
    print("\n=== 3) 日度合成统计 ===")
    for tag, fwd in (("T+5", fwd5), ("T+20", fwd20)):
        s = ic_stats(day_ic(comp, fwd))
        print(f"全样本 {tag}: IC {s['ic']:+.4f} / ICIR {s['icir']:+.2f} / "
              f"win {s['win']} / n {s['n']}")
    for yr in sorted(comp["date"].dt.year.unique()):
        sub = comp[comp["date"].dt.year == yr]
        s20 = ic_stats(day_ic(sub, fwd20))
        print(f"{yr}: T+20 {s20['ic']:+.4f}/{s20['icir']:+.2f} "
              f"({s20['n']} 日)")

    # 4) 观察仓台账
    print("\n=== 4) 最新截面台账（Top/Bottom 20）===")
    last = comp[comp["date"] == comp["date"].max()].sort_values("value")
    print(f"截面日: {comp['date'].max().date()}，{len(last)} 只")
    print("Top 20:", ", ".join(last.tail(20)["code"].tolist()))
    print("Bottom 20:", ", ".join(last.head(20)["code"].tolist()))

    print("\n完成。")


if __name__ == "__main__":
    main()
