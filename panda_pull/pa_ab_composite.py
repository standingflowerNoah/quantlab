# -*- coding: utf-8 -*-
"""PandaAI 批次组合层 A/B：pa 合成 vs PROD 复现 vs HF5 复现（月频 top100 等权）
=====================================================================
HF5 终审同款流程（scripts/hf_ab_composite.py 克隆），全部无锁：
因子湖 + mirror/kline_daily，L0 方向自动定向（pa_ 无 L0 → 手工正号，
依据 FDR 审计快照 rank_ic20 全为正：040/044/088/5d_min_low 均 +）。

  五臂（月末建仓 top100 等权，持有到下月末）：
    PA4   = pa_a101_040/044/088 + pa_5d_min_low_ratio 等权 rank-z（含观察档）
    PA3   = pa_a101_040/044/088 等权 rank-z（正选 3）
    PROD  = size + amihud_20 等权 rank-z（生产口径复现）
    HF5   = hf_dsem/amihud/vampp/rfirst30/rku 等权 rank-z（HF5 终审复现）
    PA4P  = PA4 四因子 + PROD 两因子 六因子等权（融合，测组合层增量）
    BASE  = 全池等权月收益（基准）
  成本：单边 10/20/30bp，net = gross − 2×u×bp（u=top100 月单边换手）。
  妖股月稳健性：2023-10/2023-11 单月毛收益单列（PROD 表层优势来源）。

口径声明：无涨跌停/停牌过滤、复权价、月末收盘调仓、池=ashare_ex。
诚实执行允许负面结论。

用法：python panda_pull/pa_ab_composite.py
产出：reports/_tmp/pa_ab_composite_summary.csv
      reports/_tmp/pa_ab_composite_monthly.csv
"""
from __future__ import annotations

import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FACTOR_DIR = ROOT / "data/lake/factor"
MIRROR = str(ROOT / "data/lake/clean/mirror/kline_daily.parquet")
METRIC_GLOB = str(ROOT / "data/lake/factor_metric_daily/ashare_ex/part-*.parquet")
OUT_SUM = ROOT / "reports/_tmp/pa_ab_composite_summary.csv"
OUT_MON = ROOT / "reports/_tmp/pa_ab_composite_monthly.csv"

PA4 = ["pa_a101_040", "pa_a101_044", "pa_a101_088", "pa_5d_min_low_ratio"]
PA3 = ["pa_a101_040", "pa_a101_044", "pa_a101_088"]
HF5 = ["hf_dsem_20", "hf_amihud_20", "hf_vampp_20", "hf_rfirst30_20",
       "hf_rku_20"]
PRODF = ["size", "amihud_20"]
TOPN = 100
COST_BPS = (10, 20, 30)
DEMON_MONTHS = ("2023-10", "2023-11")

CON = duckdb.connect()
CON.execute("SET memory_limit='2GB'")
CON.execute("SET threads=4")
CON.execute("SET preserve_insertion_order=false")


def month_end_dates(dates: pd.Series) -> list:
    s = pd.Series(pd.to_datetime(dates)).drop_duplicates().sort_values()
    return list(s.groupby(s.dt.to_period("M")).max())


def load_me_values(factors: list[str], me_list: list) -> pd.DataFrame:
    dl = ",".join(f"'{pd.Timestamp(d).date()}'" for d in me_list)
    frames = []
    for fac in factors:
        f = CON.execute(f"""
            SELECT CAST(date AS DATE) AS date, code, value
            FROM read_parquet('{(FACTOR_DIR / fac).as_posix()}/*.parquet')
            WHERE CAST(date AS DATE) IN ({dl})
        """).fetchdf()
        f["factor"] = fac
        frames.append(f)
    return pd.concat(frames, ignore_index=True)


def load_signs_auto(factors: list[str]) -> dict[str, float]:
    """有 L0 的因子按 rank_ic20 均值符号定向。"""
    lst = ",".join(f"'{f}'" for f in factors)
    df = CON.execute(f"""
        SELECT factor, avg(rank_ic20) AS ic FROM read_parquet('{METRIC_GLOB}')
        WHERE factor IN ({lst}) GROUP BY factor
    """).fetchdf()
    return {r.factor: float(np.sign(r.ic)) for r in df.itertuples()}


def load_fwd(me_list: list) -> pd.DataFrame:
    """月末复权价矩阵 → 下月收益矩阵（index=月末 date, columns=code）。"""
    dl = ",".join(f"'{pd.Timestamp(d).date()}'" for d in me_list)
    px = CON.execute(f"""
        SELECT CAST(date AS DATE) AS date, code,
               close * adj_factor AS c
        FROM read_parquet('{MIRROR}')
        WHERE CAST(date AS DATE) IN ({dl})
    """).fetchdf()
    mat = px.pivot(index="date", columns="code", values="c").sort_index()
    return mat.shift(-1) / mat - 1


def rank_z_score(me: pd.DataFrame, factors: list[str],
                 signs: dict) -> pd.Series:
    """月末截面 rank-z（方向化）等权平均合成 → date×code Series。"""
    piv = me.pivot_table(index=["date", "code"], columns="factor",
                         values="value")
    tot = None
    cnt = None
    for f in factors:
        z = piv[f].groupby(level=0).transform(
            lambda x: (x.rank() - x.mean()) / x.std()) * signs[f]
        zc = z.notna().astype(float)
        tot = z.fillna(0) if tot is None else tot + z.fillna(0)
        cnt = zc if cnt is None else cnt + zc
    return (tot / cnt.replace(0, np.nan)).rename("score")


def perf(r: pd.Series, base: pd.Series | None = None) -> dict:
    r = r.dropna()
    n = len(r)
    eq = (1 + r).cumprod()
    out = {
        "n_months": n,
        "CAGR": (eq.iloc[-1]) ** (12 / n) - 1 if n else np.nan,
        "vol": r.std() * np.sqrt(12) if n > 1 else np.nan,
        "sharpe": r.mean() / r.std() * np.sqrt(12) if n > 1 else np.nan,
        "mdd": float((eq / eq.cummax() - 1).min()),
    }
    if base is not None:
        b = base.reindex(r.index).dropna()
        ex = r - b
        out["ex_ann"] = ex.mean() * 12
        out["win_vs_base"] = float((ex > 0).mean())
    return out


def chain_turnover(mask: pd.DataFrame) -> pd.Series:
    """逐月单边换手 = 1 − 与上月 top100 重叠率。"""
    sets = [set(row.dropna().index) for _, row in mask.replace(False, np.nan).iterrows()]
    u = [np.nan]
    for i in range(1, len(sets)):
        a, b = sets[i - 1], sets[i]
        u.append(1.0 - len(a & b) / max(1, len(b)))
    return pd.Series(u, index=mask.index)


def main() -> None:
    t0 = time.time()
    allf = sorted(set(PA4 + PRODF + HF5))
    me0 = CON.execute(f"""
        SELECT date FROM read_parquet(
            '{(FACTOR_DIR / PA4[0]).as_posix()}/*.parquet')""").fetchdf()["date"]
    me_list = month_end_dates(me0)
    print(f"月末 {len(me_list)} 个: {me_list[0].date()} ~ {me_list[-1].date()}",
          flush=True)

    signs = load_sign_auto_safe(allf)
    print("方向符号:", signs, flush=True)

    print("加载月末截面...", flush=True)
    me = load_me_values(allf, me_list)
    fwd = load_fwd(me_list)
    print(f"  截面 {len(me):,} 行 / fwd 矩阵 {fwd.shape} "
          f"({time.time()-t0:.0f}s)", flush=True)

    arms = {
        "PA4": rank_z_score(me, PA4, signs),
        "PA3": rank_z_score(me, PA3, signs),
        "PROD": rank_z_score(me, PRODF, signs),
        "HF5": rank_z_score(me, HF5, signs),
        "PA4P": rank_z_score(me, sorted(set(PA4 + PRODF)), signs),
    }

    monthly = pd.DataFrame(index=fwd.index)
    monthly["BASE"] = fwd.mean(axis=1)
    turns = {}
    for name, sc in arms.items():
        mat = sc.unstack("code").reindex(fwd.index)
        mask = mat.rank(axis=1, ascending=False) <= TOPN
        gross = fwd.where(mask).mean(axis=1)
        monthly[f"{name}_gross"] = gross
        turns[name] = chain_turnover(mask)
    print(f"回测完成 ({time.time()-t0:.0f}s)", flush=True)

    # ---------- 汇总
    prod_m = monthly["PROD_gross"]
    hf5_m = monthly["HF5_gross"]
    rows = []
    for name in ["PA4", "PA3", "PA4P", "PROD", "HF5", "BASE"]:
        r = monthly[f"{name}_gross"] if name != "BASE" else monthly["BASE"]
        p = perf(r, base=monthly["BASE"] if name != "BASE" else None)
        row = {"arm": name, **{k: (round(v, 4) if isinstance(v, float) else v)
                               for k, v in p.items()}}
        if name != "BASE":
            u = turns[name].mean()
            row["u_top100"] = round(u, 3)
            row["corr_vs_PROD"] = round(float(r.corr(prod_m)), 3)
            row["corr_vs_HF5"] = round(float(r.corr(hf5_m)), 3)
            row["corr_vs_BASE"] = round(float(r.corr(monthly["BASE"])), 3)
            for bp in COST_BPS:
                net = r - 2 * turns[name].fillna(0) * bp / 1e4
                row[f"net_cagr_{bp}bp"] = round(
                    perf(net.dropna())["CAGR"], 4)
        rows.append(row)
    out = pd.DataFrame(rows)
    OUT_SUM.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_SUM, index=False, encoding="utf-8-sig")

    mon = monthly.copy()
    for name, u in turns.items():
        mon[f"{name}_turnover"] = u
    mon.to_csv(OUT_MON, encoding="utf-8-sig")

    print(f"\n汇总 → {OUT_SUM}")
    print(out.to_string(index=False))

    yr = mon[[c for c in mon.columns if c.endswith("_gross")
              or c == "BASE"]].groupby(mon.index.year).apply(
        lambda g: (1 + g).prod() - 1)
    print("\n分年毛收益:")
    print(yr.round(4).to_string())

    # ---------- 妖股月稳健性（2023-10/11 单月毛收益）
    print("\n妖股月单月毛收益（PROD 表层优势来源检验）:")
    dm = mon.loc[[d for d in mon.index
                  if str(d)[:7] in DEMON_MONTHS]]
    cols = [c for c in mon.columns if c.endswith("_gross")] + ["BASE"]
    print(dm[cols].round(4).to_string())


def load_sign_auto_safe(allf: list[str]) -> dict[str, float]:
    """L0 有则自动定向，无（pa_ 族）则手工 +1（FDR 审计 rank_ic20 全正）。"""
    signs = {f: 1.0 for f in allf if f.startswith("pa_")}
    rest = [f for f in allf if f not in signs]
    try:
        auto = load_signs_auto(rest)
        signs.update(auto)
        missing = [f for f in rest if f not in auto]
        if missing:
            print(f"  [warn] L0 缺失，默认 +1: {missing}")
            signs.update({f: 1.0 for f in missing})
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] L0 读取失败（{e}），全部默认 +1")
        signs.update({f: 1.0 for f in rest})
    return signs


if __name__ == "__main__":
    main()
