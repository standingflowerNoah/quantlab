"""生产融合扩展 A/B：Alpha191 卫星池（中性残差）接入检验
======================================================================
背景：Alpha191 walk-forward 通过（OOS ICIR 保留前视上界 98%），但
① 中性化衰减 50-79%——alpha 部分来自风格暴露，而生产核心已含
  size/amihud，直接叠加会双重计费；
② 故以【中性化残差】形态接入：8 因子（top10 剔除方向不稳的
  alpha016/alpha026）对 size/turnover/volatility_20 逐日 OLS 残差，
  截面 z-score 等权合成（wf eq 口径近似，OOS IC +0.027）。

变体：
  BASE   = PROD_SI = [size, amihud_20, sue_i, overnight_mom_20]（现最优直加组）
  W1     = PROD_SI + a191_resid8 等权直加（5 因子）
  W2     = 0.6×rank(核心 size+amihud) + 0.4×rank(sue_i+overnight+resid8)
判定：多相位 4 相位配对，跨窗口全正才支持接入。
输出：reports/batch7_ab_20260907.json
"""
import sys
sys.path.insert(0, '.')

import json
import time
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from quantlab.model import build_composite, run_multiphase_backtest

N, REB, PHASES = 100, 20, 4
WINDOWS = {"focus": "2025-05-01", "full": "2022-07-01"}
END = "2026-09-04"
STYLES = ["size", "turnover", "volatility_20"]
ALPHAS8 = ["alpha013", "alpha015", "alpha126", "alpha055", "alpha119",
           "alpha183", "alpha148", "alpha116"]
ALPHA_DIR = "data/lake/factor"
_MIRROR = "data/lake/clean/mirror/kline_daily.parquet"


def zscore_s(g: pd.Series) -> pd.Series:
    s = g.std()
    return (g - g.mean()) / s if s and np.isfinite(s) else g * np.nan


def load_factor(name: str) -> pd.DataFrame:
    fv = pd.read_parquet(f"{ALPHA_DIR}/{name}") if False else None
    import glob
    files = sorted(glob.glob(f"{ALPHA_DIR}/{name}/part-*.parquet"))
    fv = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    fv["date"] = pd.to_datetime(fv["date"])
    fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
    return fv.dropna(subset=["value"])[["date", "code", "value"]]


def neutralize(fv, st, min_n=100):
    """逐日 OLS 风格中性化残差（对齐 alpha191_deep.py）"""
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
    return pd.concat(out, ignore_index=True)


def mix_score(sa, sb, w):
    m = (sa.rename(columns={"score": "s1"})
         .merge(sb.rename(columns={"score": "s2"}), on=["date", "code"],
                how="inner"))
    m["r1"] = m.groupby("date")["s1"].rank(pct=True)
    m["r2"] = m.groupby("date")["s2"].rank(pct=True)
    m["score"] = w * m["r1"] + (1 - w) * m["r2"]
    return m[["date", "code", "score"]]


def yearly(curve):
    c = curve.copy()
    c["year"] = pd.to_datetime(c["date"]).dt.year
    return {int(y): round(float((1 + g["ret"]).prod() - 1), 4)
            for y, g in c.groupby("year")}


def pack(res):
    m, e = res["metrics"], res["excess"]
    return {
        "annual": m["annual_return"], "sharpe": m["sharpe"],
        "mdd": m["max_drawdown"], "excess": e["excess_annual"],
        "ir": e["information_ratio"], "yearly": yearly(res["curve"]),
        "phase_detail": res["phase_detail"],
    }


def main():
    t0 = time.time()
    print("[残差] 构建中性化残差组合（8 alpha）...", flush=True)
    st = load_factor(STYLES[0]).rename(columns={"value": STYLES[0]})
    for c in STYLES[1:]:
        st = st.merge(load_factor(c).rename(columns={"value": c}),
                      on=["date", "code"], how="outer")
    st = st.sort_values(["date", "code"])
    resid = None
    for n in ALPHAS8:
        r = neutralize(load_factor(n), st)
        r["value"] = r.groupby("date")["value"].transform(zscore_s)
        r = r.rename(columns={"value": n})
        resid = r if resid is None else resid.merge(r, on=["date", "code"],
                                                    how="inner")
    cols = ALPHAS8
    resid["value"] = resid[cols].mean(axis=1)
    a191 = resid[["date", "code", "value"]].rename(
        columns={"value": "score"}).dropna()
    print(f"  a191_resid8: {len(a191)} 行 "
          f"({a191['date'].min().date()} ~ {a191['date'].max().date()})",
          flush=True)

    print("[合成] 构建各变体...", flush=True)
    core = build_composite(["size", "amihud_20"], universe="ashare_ex")
    prod_si = build_composite(["size", "amihud_20", "sue_i",
                               "overnight_mom_20"], universe="ashare_ex")
    # W1: PROD_SI + a191_resid8 直加（rank 后等权）
    w1 = prod_si.rename(columns={"score": "s1"}).merge(
        a191.rename(columns={"score": "s2"}), on=["date", "code"], how="inner")
    w1["r1"] = w1.groupby("date")["s1"].rank(pct=True)
    w1["r2"] = w1.groupby("date")["s2"].rank(pct=True)
    w1["score"] = 0.8 * w1["r1"] + 0.2 * w1["r2"]   # 主组合为主，残差为辅
    w1 = w1[["date", "code", "score"]]
    # W2: 融合式 0.6×core + 0.4×(sue_i+overnight+resid8)
    new_si = build_composite(["sue_i", "overnight_mom_20"],
                             universe="ashare_ex")
    new3 = new_si.merge(a191.rename(columns={"score": "a191"}),
                        on=["date", "code"], how="inner")
    new3["r1"] = new3.groupby("date")["score"].rank(pct=True)
    new3["r2"] = new3.groupby("date")["a191"].rank(pct=True)
    new3["score"] = 0.5 * new3["r1"] + 0.5 * new3["r2"]
    new3 = new3[["date", "code", "score"]]
    v3_w = mix_score(core, new3, w=0.6)

    scores = {"BASE": prod_si, "W1": w1, "W2": v3_w}
    for k, v in scores.items():
        print(f"  {k}: {len(v)} 行 {v['date'].nunique()} 天", flush=True)

    out = {"config": {"n": N, "reb": REB, "phases": PHASES,
                      "windows": WINDOWS, "end": END,
                      "alphas8": ALPHAS8,
                      "note": "W1=PROD_SI+0.2×resid8; W2=0.6core+0.4(新3组)"}}
    for wname, wstart in WINDOWS.items():
        out[wname] = {}
        for tag, sc in scores.items():
            r = run_multiphase_backtest(sc.copy(), n_stocks=N, rebalance=REB,
                                        phases=PHASES, start=wstart, end=END)
            out[wname][tag] = pack(r)
            m, e = r["metrics"], r["excess"]
            print(f"  {wname}/{tag:5s} 年化{m['annual_return']:+7.1%} "
                  f"夏普{m['sharpe']:5.2f} 超额{e['excess_annual']:+7.1%} "
                  f"IR{e['information_ratio']:+5.2f}", flush=True)
        for pname, (a, b) in {"W1_vs_BASE": ("BASE", "W1"),
                              "W2_vs_BASE": ("BASE", "W2")}.items():
            da, db = out[wname][a], out[wname][b]
            deltas = [y["excess_annual"] - x["excess_annual"]
                      for x, y in zip(da["phase_detail"], db["phase_detail"])]
            print(f"  Δ{pname}: 年化{db['annual']-da['annual']:+.1%} "
                  f"IR{db['ir']-da['ir']:+.2f} "
                  f"相位超额Δ={['%+.1f%%' % (d*100) for d in deltas]}", flush=True)
        out[wname]["pair_deltas"] = {
            pname: {"annual": out[wname][b]["annual"] - out[wname][a]["annual"],
                    "ir": out[wname][b]["ir"] - out[wname][a]["ir"],
                    "phase_excess_delta": [
                        y["excess_annual"] - x["excess_annual"]
                        for x, y in zip(out[wname][a]["phase_detail"],
                                        out[wname][b]["phase_detail"])]}
            for pname, (a, b) in {"W1_vs_BASE": ("BASE", "W1"),
                                  "W2_vs_BASE": ("BASE", "W2")}.items()}

    with open("reports/batch7_ab_20260907.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → reports/batch7_ab_20260907.json")


if __name__ == "__main__":
    main()
