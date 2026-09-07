"""第五批组合 A/B：walk-forward 合成因子接入 PROD_SI（生产形态检验）
======================================================================
背景：
  batch7 已否决【静态 8-alpha 中性化残差】接入（W1/W2 全窗口 8 相位全负）。
  本批检验【walk-forward 生产形态】a191_wf_composite —— 与 batch7 的
  关键差异：wf 形态无前视、无全样本选择偏差（resid8 的因子集是全样本
  IC 排序选出的 top10），且每日现算可上线。

  互补性检验（scripts/composite_complementarity.py, 2026-09-07）证实：
  - a191_wf ~ size +0.001 / ~ amihud_20 -0.033 —— 与生产核心几乎正交，
    "双重计费"担忧在生产 wf 形态下不成立（静态残差才有风格暴露）
  - hf_wf ~ a191 +0.157 —— 两族合成基本正交
  - a191+hf 等权联合 IC +0.0460 vs a191 单独 +0.0349

变体（卫星接入统一 20% 权重，与 batch7 W1 的 0.8/0.2 可比）：
  BASE = PROD_SI = [size, amihud_20, sue_i, overnight_mom_20]（现最优）
  W1   = BASE + 0.2×rank(a191_wf)     （直加对照，对应 batch7 W1）
  W2   = 0.6×rank(core) + 0.4×[0.5×rank(sue_i+overnight) + 0.5×rank(a191_wf)]
                                        （融合结构，对应 batch7 W2）
  W3   = BASE + 0.2×rank(hf_wf)      （hf 族观察仓的组合级证据）
  W4   = BASE + 0.2×rank(0.5·a191_wf + 0.5·hf_wf)（两族联合卫星）

覆盖处理：卫星因子缺失日【回退纯 BASE 评分】（非 inner 截断），
保证 W 与 BASE 同一回测起点，full 窗口 pair delta 干净 —— 比 batch7
的 inner-merge 口径更严格（batch7 full 段实际起点受 resid8 覆盖约束）。

判定：多相位 4 相位配对，跨窗口全正才支持接入。
输出：reports/batch8_ab_20260907.json
"""
import sys
sys.path.insert(0, '.')

import glob
import json
import time
import warnings
warnings.filterwarnings("ignore")

import duckdb
import numpy as np
import pandas as pd

# ── 并行会话持写锁时强制只读连接 + 外层锁重试（本脚本纯读，安全）──
import time as _time
from quantlab.data.store import Store as _Store
_orig_new = _Store.__new__
def _ro_new(cls, readonly=False, wait_lock=True):
    for _i in range(90):                       # 60s×90 ≈ 90 分钟
        try:
            return _orig_new(cls, True, False)  # 单次快速失败，节奏外层控制
        except Exception:
            if _i == 0:
                print("[锁] quant.duckdb 被占（流水线运行中），"
                      "每 60s 重试…", flush=True)
            _time.sleep(60)
    raise RuntimeError("等待 DuckDB 锁超时（90 分钟）")
_Store.__new__ = _ro_new

from quantlab.model import build_composite, run_multiphase_backtest

N, REB, PHASES = 100, 20, 4
WINDOWS = {"focus": "2025-05-01", "full": "2022-07-01"}
END = "2026-09-04"
FACTOR_DIR = "data/lake/factor"
_con = duckdb.connect()


def load_factor(name: str) -> pd.DataFrame:
    g = f"{FACTOR_DIR}/{name}/part-*.parquet".replace("\\", "/")
    fv = _con.execute(f"SELECT date, code, value FROM read_parquet('{g}')").df()
    fv["date"] = pd.to_datetime(fv["date"])
    fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
    return fv.dropna(subset=["value"])[["date", "code", "value"]]


def fwd_returns(horizon):
    kl = "data/lake/clean/mirror/kline_daily.parquet".replace("\\", "/")
    df = _con.execute(f"""
        SELECT date, code, c_lead / c - 1 AS fwd FROM (
            SELECT date, code, close * adj_factor AS c,
                   LEAD(close * adj_factor, {horizon}) OVER (
                       PARTITION BY code ORDER BY date) AS c_lead
            FROM read_parquet('{kl}'))
        WHERE c_lead IS NOT NULL AND c > 0
    """).df()
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


def satellite(base, sat, w=0.2):
    """BASE + w×rank(sat)；sat 缺失日回退纯 BASE（覆盖完整）"""
    m = (base.rename(columns={"score": "s1"})
         .merge(sat.rename(columns={"value": "s2"}), on=["date", "code"],
                how="left"))
    m["r1"] = m.groupby("date")["s1"].rank(pct=True)
    m["r2"] = m.groupby("date")["s2"].rank(pct=True)
    m["score"] = m["r1"]
    has = m["r2"].notna()
    m.loc[has, "score"] = w * m.loc[has, "r2"] + (1 - w) * m.loc[has, "r1"]
    return m[["date", "code", "score"]]


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
    a191 = load_factor("a191_wf_composite")
    hf = load_factor("hf_wf_composite")
    print(f"a191_wf: {a191['date'].min().date()}~{a191['date'].max().date()}"
          f"（{len(a191):,} 行）", flush=True)
    print(f"hf_wf:   {hf['date'].min().date()}~{hf['date'].max().date()}"
          f"（{len(hf):,} 行）", flush=True)

    # 头部对照：重叠期 a191 单独（补互补性检验的解读基准）
    ov = a191[a191["date"] >= hf["date"].min()]
    ic = day_ic(ov, fwd_returns(20))
    print(f"[对照] 重叠期 a191 单独 T+20: IC {ic.mean():+.4f} / "
          f"ICIR {ic.mean() / ic.std():+.2f}（n={len(ic)}）", flush=True)

    print("[合成] 构建各变体...", flush=True)
    base = build_composite(["size", "amihud_20", "sue_i", "overnight_mom_20"],
                           universe="ashare_ex")
    core = build_composite(["size", "amihud_20"], universe="ashare_ex")
    new_si = build_composite(["sue_i", "overnight_mom_20"],
                             universe="ashare_ex")

    w1 = satellite(base, a191, w=0.2)

    # W2: 0.6×core + 0.4×(0.5×new_si + 0.5×a191，a191 缺失日回退 new_si)
    new3 = (new_si.rename(columns={"score": "s1"})
            .merge(a191.rename(columns={"value": "s2"}), on=["date", "code"],
                   how="left"))
    new3["r1"] = new3.groupby("date")["s1"].rank(pct=True)
    new3["r2"] = new3.groupby("date")["s2"].rank(pct=True)
    new3["score"] = new3["r1"]
    has = new3["r2"].notna()
    new3.loc[has, "score"] = (0.5 * new3.loc[has, "r2"]
                              + 0.5 * new3.loc[has, "r1"])
    w2 = mix_score(core, new3[["date", "code", "score"]], w=0.6)

    w3 = satellite(base, hf, w=0.2)

    # W4: 联合卫星 0.5·a191 + 0.5·hf（hf 缺失日回退 a191）
    j = (a191.rename(columns={"value": "va"})
         .merge(hf.rename(columns={"value": "vh"}), on=["date", "code"],
                how="left"))
    j["ra"] = j.groupby("date")["va"].rank(pct=True)
    j["rh"] = j.groupby("date")["vh"].rank(pct=True)
    j["value"] = j["ra"]
    both = j["rh"].notna()
    j.loc[both, "value"] = 0.5 * j.loc[both, "ra"] + 0.5 * j.loc[both, "rh"]
    w4 = satellite(base, j[["date", "code", "value"]], w=0.2)

    scores = {"BASE": base, "W1": w1, "W2": w2, "W3": w3, "W4": w4}
    for k, v in scores.items():
        print(f"  {k}: {len(v)} 行 {v['date'].nunique()} 天 "
              f"({v['date'].min().date()}~{v['date'].max().date()})",
              flush=True)

    out = {"config": {
        "n": N, "reb": REB, "phases": PHASES, "windows": WINDOWS,
        "end": END, "sat_weight": 0.2,
        "note": ("W1=BASE+0.2a191wf; W2=0.6core+0.4(0.5new_si+0.5a191wf); "
                 "W3=BASE+0.2hfwf; W4=BASE+0.2(0.5a191+0.5hf); "
                 "卫星缺失日回退BASE（覆盖完整口径）")}}
    pairs = {f"W{i}_vs_BASE": ("BASE", f"W{i}") for i in (1, 2, 3, 4)}
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
        for pname, (a, b) in pairs.items():
            da, db = out[wname][a], out[wname][b]
            deltas = [y["excess_annual"] - x["excess_annual"]
                      for x, y in zip(da["phase_detail"], db["phase_detail"])]
            print(f"  Δ{pname}: 年化{db['annual']-da['annual']:+.1%} "
                  f"IR{db['ir']-da['ir']:+.2f} "
                  f"相位超额Δ={['%+.1f%%' % (d*100) for d in deltas]}",
                  flush=True)
        out[wname]["pair_deltas"] = {
            pname: {"annual": out[wname][b]["annual"] - out[wname][a]["annual"],
                    "ir": out[wname][b]["ir"] - out[wname][a]["ir"],
                    "phase_excess_delta": [
                        y["excess_annual"] - x["excess_annual"]
                        for x, y in zip(out[wname][a]["phase_detail"],
                                        out[wname][b]["phase_detail"])]}
            for pname, (a, b) in pairs.items()}

    with open("reports/batch8_ab_20260907.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → reports/batch8_ab_20260907.json")


if __name__ == "__main__":
    main()
