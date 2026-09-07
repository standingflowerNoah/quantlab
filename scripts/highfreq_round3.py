#!/usr/bin/env python3
"""高频因子第三轮评估：聪明钱月度平滑 + 放量时刻族（待著而救）
用法: python scripts/highfreq_round3.py
对应研究报告 7.3 优先方向 ③④

直接从 minute_feat 宽表 parquet 计算（绕开 quant.duckdb 写锁），
与 factor/library/highfreq.py 的 _HfFactor 口径一致：
  hf_smartq_20   = smart_q 的 20 日均值（min_valid 15）
  hf_topvr_20    = topv_r 的 20 日均值
  hf_topvupr_20  = topv_upr 的 20 日均值
宽表行门槛 n_min >= 120 且 rv > 0，QUALIFY ISFINITE。

输出：raw/风格中性 IC（T+5/T+20）、2025/2026 分段、5 分层多空（T+20）、
      smartq_10 vs smartq_20 对照（开源口径 rankIC ≈ -0.062）。
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


def zscore_s(g: pd.Series) -> pd.Series:
    s = g.std()
    return (g - g.mean()) / s if s and np.isfinite(s) else g * np.nan


STYLES = ["size", "turnover", "volatility_20"]


def load_factor(name):
    g = _p(f"factor/{name}") + "/part-*.parquet"
    fv = q(f"SELECT date, code, value FROM read_parquet('{g}')")
    fv["date"] = pd.to_datetime(fv["date"])
    fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
    return fv.dropna(subset=["value"])


def load_styled():
    st = load_factor(STYLES[0]).rename(columns={"value": STYLES[0]})
    for c in STYLES[1:]:
        st = st.merge(load_factor(c).rename(columns={"value": c}),
                      on=["date", "code"], how="outer")
    return st


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


# ── 宽表直算（复刻 _HfFactor 的 SQL 语义）────────────────────────
def from_wide(col: str, w: int = 20, mv: int = 15) -> pd.DataFrame:
    g = _p("clean/minute_feat") + "/part-*.parquet"
    df = q(f"""
        SELECT date, code,
               CASE WHEN COUNT({col}) OVER wnd >= {mv}
                    THEN AVG({col}) OVER wnd END AS value
        FROM (
            SELECT m.date, m.code, m.{col}, m.n_min, m.rv
            FROM read_parquet('{g}') m
            JOIN read_parquet('{_p('clean/mirror/kline_daily.parquet')}') k
              ON k.date = m.date AND k.code = m.code
            WHERE m.n_min >= 120 AND m.rv > 0)
        WINDOW wnd AS (PARTITION BY code ORDER BY date
                       ROWS BETWEEN {w - 1} PRECEDING AND CURRENT ROW)
        QUALIFY value IS NOT NULL AND ISFINITE(value)
    """)
    df["date"] = pd.to_datetime(df["date"])
    return df.dropna(subset=["value"])


def ic_series(fv, fwd, min_n=30):
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


def neutralize(fv, st, min_n=100):
    df = fv.merge(st, on=["date", "code"], how="left")
    for c in STYLES:
        df[c] = df.groupby("date")[c].transform(
            lambda x: x.rank(pct=True) - 0.5)
    df = df.dropna(subset=STYLES + ["value"])
    out = []
    for d, g in df.groupby("date"):
        if len(g) < min_n:
            continue
        X = np.column_stack([np.ones(len(g))] + [g[c].values for c in STYLES])
        y = g["value"].values.astype(float)
        try:
            beta = np.linalg.lstsq(X, y, rcond=None)[0]
            out.append(g[["date", "code"]].assign(value=y - X @ beta))
        except np.linalg.LinAlgError:
            continue
    return (pd.concat(out, ignore_index=True)
              .sort_values(["date", "code"]).reset_index(drop=True))


def layers(fv, fwd, n_q=5):
    """T+20 分层多空（每组等权 fwd 均值，汇总为均值/年化）"""
    m = fv.merge(fwd, on=["date", "code"], how="inner")
    rows = []
    for d, g in m.groupby("date"):
        if len(g) < n_q * 30:
            continue
        g = g.assign(qd=pd.qcut(g["value"].rank(method="first"), n_q,
                                labels=False))
        r = g.groupby("qd")["fwd"].mean()
        rows.append({"date": d, **{f"q{i}": r.get(i, np.nan)
                                   for i in range(n_q)}})
    L = pd.DataFrame(rows).set_index("date")
    ls = L[f"q{n_q - 1}"] - L["q0"]
    return L.mean(), (ls.mean() * 12, ls.std() * np.sqrt(12))


def main():
    print("宽表直算三个候选因子（20 日平滑）...")
    cand = {
        "hf_smartq_20": from_wide("smart_q", 20, 15),
        "hf_topvr_20": from_wide("topv_r", 20, 15),
        "hf_topvupr_20": from_wide("topv_upr", 20, 15),
    }
    for k, v in cand.items():
        print(f"  {k}: {len(v):,} 行")
    ref = {"hf_smartq_10": load_factor("hf_smartq_10")}

    st = load_styled()
    fwd5, fwd20 = fwd_returns(5), fwd_returns(20)

    print("\n=== 1) IC：raw / 风格中性（T+5 | T+20）===")
    print(f"{'因子':16s} {'raw T+5':>15s} {'中性 T+5':>15s} "
          f"{'raw T+20':>15s} {'中性 T+20':>15s}")
    allfv = {}
    for name, fv in {**cand, **ref}.items():
        r5 = ic_stats(ic_series(fv, fwd5))
        fv_s = neutralize(fv, st)
        n5 = ic_stats(ic_series(fv_s, fwd5))
        r20 = ic_stats(ic_series(fv, fwd20))
        n20 = ic_stats(ic_series(fv_s, fwd20))
        print(f"{name:16s} {r5['ic']:+.4f}/{r5['icir']:+.2f} "
              f"{n5['ic']:+.4f}/{n5['icir']:+.2f} "
              f"{r20['ic']:+.4f}/{r20['icir']:+.2f} "
              f"{n20['ic']:+.4f}/{n20['icir']:+.2f}")
        allfv[name] = (fv, fv_s)

    print("\n=== 2) 2025 / 2026 分段（raw T+20）===")
    print(f"{'因子':16s} {'2025':>14s} {'2026':>14s}")
    for name, (fv, _) in allfv.items():
        r = []
        for yr in (2025, 2026):
            sub = fv[fv["date"].dt.year == yr]
            s = ic_stats(ic_series(sub, fwd20))
            r.append(f"{s['ic']:+.4f}/{s['icir']:+.2f}")
        print(f"{name:16s} {r[0]:>14s} {r[1]:>14s}")

    print("\n=== 3) 5 分层多空（T+20，等权未计费用）===")
    print(f"{'因子':16s} {'Q1':>8s} {'Q5':>8s} {'多空/20d':>10s} "
          f"{'多空年化':>9s} {'月夏普':>7s}")
    for name, (fv, _) in allfv.items():
        Lm, (lsa, lss) = layers(fv, fwd20)
        print(f"{name:16s} {Lm['q0']:+8.4f} {Lm['q4']:+8.4f} "
              f"{Lm['q4'] - Lm['q0']:+10.4f} {lsa:+9.2%} "
              f"{lsa / lss if lss else np.nan:+7.2f}")

    print("\n完成。")


if __name__ == "__main__":
    main()
