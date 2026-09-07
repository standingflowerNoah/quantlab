#!/usr/bin/env python3
"""高频因子深化研究：中性化纯净检验 + 正交合成 + 分域检验
用法: python scripts/highfreq_neutral.py
对应研究报告 7.3 优先方向 ①②⑤

输出：
  1) 纯净 IC：7 因子对 (log_size, turnover, volatility_20) 风格正交、
     及 (+行业哑变量) 全中性后的 T+5/T+20 IC —— 剥离小盘/高换手暴露
  2) 中性化后因子间相关矩阵（合成正交性）
  3) 合成因子：等权 zscore（全样本）+ 2025-ICIR 加权（2026 准 OOS 验证）
  4) 分域 IC：hs300 / zz500 / zz1000 成分股内（中小盘更强假设检验）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import duckdb

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake"
_con = duckdb.connect()          # 纯内存连接：直读 parquet，绕开 quant.duckdb 写锁


def q(sql: str) -> pd.DataFrame:
    return _con.execute(sql).df()


def query(sql: str) -> pd.DataFrame:
    return q(sql.replace("'", "'"))


FACTORS = {   # name -> 方向（+1 值大预期收益高；-1 值小预期收益高）
    "hf_rsk_20": -1, "hf_dsem_20": +1, "hf_corr_rv_20": -1,
    "hf_amtrange_20": -1, "hf_rvvol_20": -1,
    "hf_amihud_20": +1, "hf_smartq_10": -1,
}
STYLES = ["size", "turnover", "volatility_20"]
DOMAINS = ["hs300", "zz500", "zz1000"]


# ── 基础工具 ─────────────────────────────────────────────────────
def _p(sub: str) -> str:
    return str(LAKE / sub).replace("\\", "/")


def zscore_s(g: pd.Series) -> pd.Series:
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


def ic_series(fv: pd.DataFrame, fwd: pd.DataFrame, min_n=30) -> pd.Series:
    m = fv.merge(fwd, on=["date", "code"], how="inner")
    out = []
    for d, g in m.groupby("date"):
        if len(g) < min_n:
            continue
        out.append(g["value"].rank().corr(g["fwd"].rank()))
    return pd.Series(out, dtype=float)


def ic_stats(ic: pd.Series):
    if len(ic) == 0 or ic.std() == 0:
        return {"n": len(ic), "ic": np.nan, "icir": np.nan, "win": np.nan}
    return {"n": len(ic), "ic": round(float(ic.mean()), 4),
            "icir": round(float(ic.mean() / ic.std()), 3),
            "win": round(float((ic > 0).mean()), 3)}


# ── 中性化：截面 OLS 残差（风格连续变量 + 可选行业哑变量）────────
def neutralize(fv: pd.DataFrame, style_dfs: dict, industry_map: dict | None,
               min_n=100) -> pd.DataFrame:
    df = fv.copy()
    for k, s in style_dfs.items():
        df = df.merge(s.rename(columns={"value": k})[["date", "code", k]],
                      on=["date", "code"], how="left")
        df[k] = df.groupby("date")[k].transform(
            lambda x: x.rank(pct=True) - 0.5)   # 截面 rank 归一（稳健）
        df = df.rename(columns={k: k})  # no-op
    if industry_map is not None:
        df["ind"] = df["code"].map(industry_map)
    else:
        df["ind"] = "ALL"

    cols = [c for c in STYLES if c in df.columns]
    df = df.dropna(subset=cols + ["value"])
    if industry_map is not None:
        df = df.dropna(subset=["ind"])

    out = []
    for d, g in df.groupby("date"):
        if len(g) < min_n:
            continue
        X = [np.ones(len(g))] + [g[c].values for c in cols]
        if industry_map is not None:
            dum = pd.get_dummies(g["ind"]).values.astype(float)
            if dum.shape[1] > 1:
                X.append(dum[:, 1:])
        X = np.column_stack(X)
        y = g["value"].values.astype(float)
        try:
            beta = np.linalg.lstsq(X, y, rcond=None)[0]
            g = g.assign(value=y - X @ beta)
        except np.linalg.LinAlgError:
            continue
        out.append(g[["date", "code", "value"]])
    return (pd.concat(out, ignore_index=True)
              .sort_values(["date", "code"]).reset_index(drop=True)
            if out else pd.DataFrame(columns=["date", "code", "value"]))


# ── 主流程 ───────────────────────────────────────────────────────
def main():
    print("载入因子与风格数据（纯 parquet 路径）...")
    fvs = {n: load_factor(n) for n in FACTORS}
    styles = {n: load_factor(n) for n in STYLES}
    ins_p = _p("clean/mirror/instruments.parquet")
    ind = q(f"SELECT code, industry FROM read_parquet('{ins_p}')")
    industry_map = dict(zip(ind["code"], ind["industry"]))
    fwd5, fwd20 = fwd_returns(5), fwd_returns(20)

    # 1) 纯净 IC（风格中性 / +行业）
    print("\n=== 1) 中性化纯净 IC（T+5 | T+20）===")
    print(f"{'因子':16s} {'原始':>14s} {'风格中性':>14s} {'+行业':>14s}")
    pure = {}
    for n, sign in FACTORS.items():
        r5 = ic_stats(ic_series(fvs[n], fwd5))
        fv_s = neutralize(fvs[n], styles, None)
        s5 = ic_stats(ic_series(fv_s, fwd5))
        fv_i = neutralize(fvs[n], styles, industry_map)
        i5 = ic_stats(ic_series(fv_i, fwd5))
        s20 = ic_stats(ic_series(fv_s, fwd20))
        print(f"{n:16s} {r5['ic']:+.4f}/{r5['icir']:+.2f} "
              f"{s5['ic']:+.4f}/{s5['icir']:+.2f} "
              f"{i5['ic']:+.4f}/{i5['icir']:+.2f}   (T+20 风格中性 "
              f"{s20['ic']:+.4f}/{s20['icir']:+.2f})")
        pure[n] = fv_s   # 后续用风格中性版

    # 2) 中性化后因子间相关（平均绝对两两秩相关）
    print("\n=== 2) 中性化后因子间相关矩阵（上三角平均）===")
    names = list(FACTORS)
    # 统一方向后再看相关
    directed = {n: pure[n].assign(value=pure[n]["value"] * s)
                for n, s in FACTORS.items()}
    cors = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            m = directed[a].merge(directed[b], on=["date", "code"],
                                  suffixes=("_a", "_b"))
            cc = m.groupby("date").apply(
                lambda g: g["value_a"].rank().corr(g["value_b"].rank()))
            cors[(a, b)] = round(float(cc.mean()), 3)
    vals = [abs(v) for v in cors.values()]
    print(f"两两 |corr| 均值: {np.mean(vals):.3f} / 最大: {np.max(vals):.3f}")
    top = sorted(cors.items(), key=lambda kv: -abs(kv[1]))[:5]
    for (a, b), v in top:
        print(f"  {a} ~ {b}: {v:+.3f}")

    # 3) 合成
    print("\n=== 3) 合成因子 ===")
    # 3a) 等权 zscore（全样本）
    zs = []
    for n, fv in directed.items():
        z = fv.groupby("date")["value"].transform(zscore_s)
        zs.append(fv.assign(value=z))
    base = zs[0][["date", "code"]].copy()
    for i, z in enumerate(zs):
        base = base.merge(z[["date", "code", "value"]],
                          on=["date", "code"], how="inner",
                          suffixes=("", f"_{i}"))
    vcols = [c for c in base.columns if c.startswith("value")]
    base["value"] = base[vcols].mean(axis=1)
    eq = base[["date", "code", "value"]].dropna()
    e5, e20 = ic_stats(ic_series(eq, fwd5)), ic_stats(ic_series(eq, fwd20))
    print(f"等权合成（7 因子，方向统一后 zscore）: "
          f"T+5 IC {e5['ic']:+.4f}/ICIR {e5['icir']:+.2f}/win {e5['win']} | "
          f"T+20 IC {e20['ic']:+.4f}/ICIR {e20['icir']:+.2f}")

    # 分年（等权合成稳健性）
    for yr in (2025, 2026):
        sub = eq[eq["date"].dt.year == yr]
        y5 = ic_stats(ic_series(sub, fwd5))
        print(f"  {yr}: T+5 IC {y5['ic']:+.4f}/ICIR {y5['icir']:+.2f}")

    # 3b) 2025-ICIR 加权 → 2026 验证（准 OOS）
    w = {}
    for n, fv in directed.items():
        sub25 = fv[fv["date"].dt.year == 2025]
        s25 = ic_stats(ic_series(sub25, fwd5))
        w[n] = s25["icir"] if np.isfinite(s25["icir"]) else 0.0
    wsum = sum(abs(v) for v in w.values()) or 1.0
    zs = []
    for n, fv in directed.items():
        z = fv.groupby("date")["value"].transform(zscore_s)
        zs.append(fv.assign(value=z * (w[n] / wsum) * np.sign(w[n] or 1)))
    base = zs[0][["date", "code"]].copy()
    for i, z in enumerate(zs):
        base = base.merge(z[["date", "code", "value"]],
                          on=["date", "code"], how="inner",
                          suffixes=("", f"_{i}"))
    vcols = [c for c in base.columns if c.startswith("value")]
    base["value"] = base[vcols].sum(axis=1)
    iw = base[["date", "code", "value"]].dropna()
    i26 = iw[iw["date"].dt.year == 2026]
    r26 = ic_stats(ic_series(i26, fwd5))
    print(f"ICIR 加权（2025 估权 → 2026 验证）: T+5 IC {r26['ic']:+.4f}/"
          f"ICIR {r26['icir']:+.2f}/win {r26['win']}")

    # 4) 分域检验（风格中性版 + 等权合成）
    print("\n=== 4) 分域 IC（T+5，风格中性后）===")
    imp = _p("clean/mirror/index_members.parquet")
    im = q(f"SELECT index_code, code FROM read_parquet('{imp}') "
           f"WHERE is_current = 1")
    dom_map = {d: set(im[im["index_code"] == d]["code"]) for d in DOMAINS}
    print(f"{'因子/合成':16s} {'全A':>8s} {'hs300':>8s} {'zz500':>8s} "
          f"{'zz1000':>8s}")
    test = {**{n: pure[n] for n in FACTORS}, "COMPOSITE_EQ": eq}
    for n, fv in test.items():
        row = []
        for d in DOMAINS:
            sub = fv[fv["code"].isin(dom_map[d])]
            row.append(ic_stats(ic_series(sub, fwd5, min_n=30))["ic"])
        a = ic_stats(ic_series(fv, fwd5))["ic"]
        print(f"{n:16s} {a:+.4f} " + " ".join(f"{v:+8.4f}" for v in row))

    print("\n完成。")


if __name__ == "__main__":
    main()
