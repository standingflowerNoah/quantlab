"""因子/模型系统化评估引擎 v3（factor-model-evaluator skill 执行核心）
======================================================================
五项能力：
  1. 输入参数：--type factor|model --name --start --end --universe --tags
     --horizons（多周期 IC 谱，默认 1,3,5,10,20,40,60）
  2. 评估维度：四象限完备体系（表现/稳定性/风险/真伪可用+转移效率）
  3. 统一模板报告（v3 补齐框架 §1.1/§1.2/§1.3/§8.1/§8.2 全部条目）：
     IC 衰减曲线+半衰期 / 多周期 IC-ICIR 谱 / 十分位分组 / 月度 IC /
     正交化增量残差 IC + v2 全部（判定逐项表/年度五分位/滚动/冗余/
     模型超额对照+月度表+回撤区间+成分因子证据）
  4. 命名规范：reports/factor_eval/{type}_{name}_{yyyymmdd}_{HHMM}.md
     + 索引 index.json（追加式，同日重跑不覆盖）
  5. 检索：scripts/factor_eval/search_eval.py

数据源（全只读）：factor_audit JSON / fdr_factor_ranking.json / registry /
因子湖 parquet（年度 IC+五分位+滚动，in-memory duckdb READ_ONLY ATTACH，
用后 DETACH）/ run_optimized_backtest（含 tc_avg）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import duckdb
import numpy as np
import pandas as pd

AUDIT_DIR = ROOT / "data" / "lake" / "factor_audit"
FACTOR_DIR = ROOT / "data" / "lake" / "factor"
FDR_JSON = ROOT / "reports" / "fdr_factor_ranking.json"
DB = ROOT / "data" / "quant.duckdb"
OUT_DIR = ROOT / "reports" / "factor_eval"
INDEX = OUT_DIR / "index.json"

CORE_FACTORS = ["size", "amihud_20", "sue_i", "hf_amihud_20"]
DEFAULT_HORIZONS = [1, 3, 5, 10, 20, 40, 60, 120]

MODEL_SPECS: dict[str, tuple] = {
    "PROD": (["size", "amihud_20"], "equal"),
    "EQ3": (["size", "amihud_20", "sue_i"], "equal"),
    "PROD_SI": (["size", "amihud_20", "sue_i", "overnight_mom_20"], "equal"),
    "PROD_HF": (["size", "amihud_20", "hf_amihud_20"], "equal"),
    "PROD_HFA": (["size", "amihud_20", "sue_i", "hf_amihud_20"], "equal"),
    "EQ3_HFA_ICW": (["size", "amihud_20", "sue_i", "hf_amihud_20"], "ic_weighted"),
}


# ─────────────────────────── 通用 ───────────────────────────
def load_index() -> list[dict]:
    if INDEX.exists():
        return json.loads(INDEX.read_text(encoding="utf-8"))
    return []


def save_index(idx: list[dict]) -> None:
    INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=1),
                     encoding="utf-8")


def registry_info(name: str) -> dict:
    from quantlab.factor.registry import get_factor
    try:
        f = get_factor(name)
        return {"category": f.category,
                "rationale": getattr(f, "economic_rationale", "") or "未登记",
                "description": f.description}
    except KeyError:
        return {"category": "?", "rationale": "未登记（registry 无此因子）",
                "description": ""}


def fdr_info(name: str) -> dict | None:
    if not FDR_JSON.exists():
        return None
    for r in json.loads(FDR_JSON.read_text(encoding="utf-8"))["ranking"]:
        if r["factor"] == name:
            return r
    return None


def audit_info(name: str) -> dict | None:
    fp = AUDIT_DIR / f"{name}.json"
    if not fp.exists():
        return None
    return json.loads(fp.read_text(encoding="utf-8"))


# ─────────────────── 因子：逐日序列 / 年度 / 分组 / 滚动 ───────────────────
def factor_daily_metrics(name: str, horizon: int, start: str, end: str) -> tuple:
    """返回 (daily, yearly, extra)：逐日 IC+五分位组收益；年度聚合；
    extra 含滚动 12M ICIR / 连续同号段 / 分组单调性 / 全期统计"""
    parts = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    if not parts:
        raise FileNotFoundError(f"{FACTOR_DIR / name} 无 parquet")
    plist = [str(p) for p in parts]
    con = duckdb.connect()
    con.execute(f"ATTACH '{DB}' AS maindb (READ_ONLY)")
    q = f"""
    WITH f AS (
        SELECT date, code, value FROM read_parquet({plist})
        WHERE date BETWEEN DATE '{start}' AND DATE '{end}'),
    px AS (
        SELECT date, code, close * adj_factor AS c FROM maindb.kline_daily),
    base AS (
        SELECT f.date, f.code, f.value AS v,
               LEAD(px.c, {horizon}) OVER (PARTITION BY f.code ORDER BY f.date)
                   / px.c - 1 AS fwd
        FROM f JOIN px ON f.date = px.date AND f.code = px.code),
    ranked AS (
        SELECT date, code, v, fwd,
               RANK() OVER (PARTITION BY date ORDER BY v) AS rv,
               RANK() OVER (PARTITION BY date ORDER BY fwd) AS rf,
               NTILE(5) OVER (PARTITION BY date ORDER BY v) AS quint
        FROM base WHERE fwd IS NOT NULL)
    SELECT date, corr(rv, rf) AS ic,
           AVG(CASE WHEN quint = 1 THEN fwd END) AS q1_ret,
           AVG(CASE WHEN quint = 2 THEN fwd END) AS q2_ret,
           AVG(CASE WHEN quint = 3 THEN fwd END) AS q3_ret,
           AVG(CASE WHEN quint = 4 THEN fwd END) AS q4_ret,
           AVG(CASE WHEN quint = 5 THEN fwd END) AS q5_ret,
           COUNT(*) AS n
    FROM ranked GROUP BY date ORDER BY date
    """
    daily = con.execute(q).df()
    con.execute("DETACH maindb")
    con.close()
    daily["date"] = pd.to_datetime(daily["date"])
    daily["year"] = daily["date"].dt.year
    daily["month"] = daily["date"].dt.month
    qcols = ["q1_ret", "q2_ret", "q3_ret", "q4_ret", "q5_ret"]
    g = daily.groupby("year")
    yearly = pd.DataFrame({
        "year": g.size().index,
        "n_days": g.size().values,
        "ic_mean": g["ic"].mean().round(4).values,
        "icir": (g["ic"].mean() / g["ic"].std()).round(3).values,
        "win_rate": g["ic"].apply(lambda s: (s > 0).mean()).round(3).values,
        **{c: g[c].mean().round(4).values for c in qcols},
    })
    yearly["ls_spread"] = (yearly["q5_ret"] - yearly["q1_ret"]).round(4)
    yearly["long_share"] = [
        round(l / s, 3) if (s > 0 and l > 0) else None
        for l, s in zip(yearly["q5_ret"], yearly["ls_spread"])
    ]
    # extra：滚动 / 连续段 / 单调性 / 全期
    roll = (daily["ic"].rolling(243, min_periods=120).mean()
            / daily["ic"].rolling(243, min_periods=120).std())
    sgn = (daily["ic"] > 0).astype(int).values
    runs, cur = [], 1
    for i in range(1, len(sgn)):
        if sgn[i] == sgn[i - 1]:
            cur += 1
        else:
            runs.append(cur)
            cur = 1
    runs.append(cur)
    q_means = daily[qcols].mean()
    extra = {
        "roll_icir_min": round(float(roll.min()), 3),
        "roll_icir_max": round(float(roll.max()), 3),
        "roll_icir_last": round(float(roll.iloc[-1]), 3),
        "max_run_same_sign": int(max(runs)) if runs else 0,
        "monotonicity": round(float(
            pd.Series(q_means.values).corr(
                pd.Series(range(1, 6)), method="spearman")), 3),
        "q_means_bp": {f"Q{i+1}": round(float(q_means[f"q{i+1}_ret"]) * 1e4, 1)
                       for i in range(5)},
        "ic_all": round(float(daily["ic"].mean()), 4),
        "icir_all": round(float(daily["ic"].mean() / daily["ic"].std()), 3),
        "win_all": round(float((daily["ic"] > 0).mean()), 3),
        "n_days_all": int(len(daily)),
        "monthly": daily.groupby(["year", "month"])["ic"].mean().round(4),
    }
    return daily, yearly, extra


def factor_core_corr(name: str, start: str, end: str,
                     window_days: int = 250) -> pd.DataFrame:
    """与核心因子最近 window_days 日截面 spearman 均值（冗余检查）"""
    cores = [c for c in CORE_FACTORS if c != name]
    con = duckdb.connect()
    frames = {}
    for c in [name] + cores:
        parts = sorted((FACTOR_DIR / c).glob("part-*.parquet"))
        if not parts:
            continue
        df = con.execute(f"""
            SELECT date, code, value FROM read_parquet(
                {[str(p) for p in parts]})
            WHERE date BETWEEN DATE '{start}' AND DATE '{end}'
        """).df()
        if len(df):
            frames[c] = df
    con.close()
    if name not in frames:
        return pd.DataFrame(columns=["core", "rho", "n_days"])
    dates = sorted(frames[name]["date"].unique())[-window_days:]
    base = frames[name][frames[name]["date"].isin(dates)]
    rows = []
    for c in cores:
        if c not in frames:
            continue
        m = base.merge(frames[c], on=["date", "code"], suffixes=("_a", "_b"))
        if m.empty:
            continue
        rhos = (m.groupby("date")
                .apply(lambda x: x["value_a"].corr(x["value_b"],
                                                   method="spearman")
                       if len(x) > 30 else None, include_groups=False))
        rhos = rhos.dropna()
        if len(rhos):
            rows.append({"core": c, "rho": round(float(rhos.mean()), 3),
                         "n_days": int(len(rhos))})
    return pd.DataFrame(rows)


# ─────────────── 因子：IC 衰减谱 / 十分位 / 正交化增量 ───────────────
def factor_horizon_structure(name: str, horizons: list[int],
                             start: str, end: str) -> tuple:
    """多周期 IC/ICIR/胜率谱（§1.1 IC 衰减曲线 + §8.2 衰减动力学）。
    单查询多 LEAD 列 unpivot，返回 (DataFrame[h,ic,icir,win,n_days],
    meta{peak_ic,peak_h,half_life})；半衰期=|IC| 跌破峰值一半的
    horizon（线性插值），决定信号自然换手下限。"""
    parts = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    if not parts:
        raise FileNotFoundError(f"{FACTOR_DIR / name} 无 parquet")
    plist = [str(p) for p in parts]
    hs = sorted(set(horizons))
    leads = ",\n               ".join(
        f"LEAD(px.c, {h}) OVER w / px.c - 1 AS fwd{h}" for h in hs)
    unions = "\n        UNION ALL ".join(
        f"SELECT date, code, v, {h} AS h, fwd{h} AS fwd FROM base"
        for h in hs)
    q = f"""
    WITH f AS (
        SELECT date, code, value AS v FROM read_parquet({plist})
        WHERE date BETWEEN DATE '{start}' AND DATE '{end}'),
    px AS (
        SELECT date, code, close * adj_factor AS c FROM maindb.kline_daily),
    base AS (
        SELECT f.date, f.code, f.v,
               {leads}
        FROM f JOIN px ON f.date = px.date AND f.code = px.code
        WINDOW w AS (PARTITION BY f.code ORDER BY f.date)),
    unp AS ({unions}),
    ranked AS (
        SELECT date, h, v, fwd,
               RANK() OVER (PARTITION BY date, h ORDER BY v) AS rv,
               RANK() OVER (PARTITION BY date, h ORDER BY fwd) AS rf
        FROM unp)
    SELECT h, date, corr(rv, rf) AS ic
    FROM ranked GROUP BY h, date ORDER BY h, date
    """
    con = duckdb.connect()
    con.execute(f"ATTACH '{DB}' AS maindb (READ_ONLY)")
    df = con.execute(q).df()
    con.execute("DETACH maindb")
    con.close()
    rows = []
    for h, g in df.groupby("h"):
        rows.append({
            "h": int(h), "ic": round(float(g["ic"].mean()), 4),
            "icir": round(float(g["ic"].mean() / g["ic"].std()), 3),
            "win": round(float((g["ic"] > 0).mean()), 3),
            "n_days": int(len(g)),
        })
    res = pd.DataFrame(rows).sort_values("h").reset_index(drop=True)
    peak = float(res["ic"].abs().max())
    peak_h = int(res.loc[res["ic"].abs().idxmax(), "h"])
    half = None
    tail = res[res["h"] >= peak_h].reset_index(drop=True)
    for i in range(1, len(tail)):
        if abs(tail["ic"][i]) <= peak / 2:
            ic0, ic1 = abs(tail["ic"][i - 1]), abs(tail["ic"][i])
            frac = 0.0 if ic0 == ic1 else (ic0 - peak / 2) / (ic0 - ic1)
            half = round(tail["h"][i - 1]
                         + frac * (tail["h"][i] - tail["h"][i - 1]), 1)
            break
    return res, {"peak_ic": round(peak, 4), "peak_h": peak_h,
                 "half_life": half, "max_h": int(res["h"].max())}


def factor_decile(name: str, horizon: int, start: str, end: str) -> dict:
    """十分位分组（§1.2 分组收益，D1=因子值最低 10%）。全期 D1–D10
    平均远期收益 + 十组单调性 + 多空价差。"""
    parts = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    if not parts:
        raise FileNotFoundError(f"{FACTOR_DIR / name} 无 parquet")
    plist = [str(p) for p in parts]
    con = duckdb.connect()
    con.execute(f"ATTACH '{DB}' AS maindb (READ_ONLY)")
    q = f"""
    WITH f AS (
        SELECT date, code, value FROM read_parquet({plist})
        WHERE date BETWEEN DATE '{start}' AND DATE '{end}'),
    px AS (
        SELECT date, code, close * adj_factor AS c FROM maindb.kline_daily),
    base AS (
        SELECT f.date, f.code, f.value AS v,
               LEAD(px.c, {horizon}) OVER (PARTITION BY f.code ORDER BY f.date)
                   / px.c - 1 AS fwd
        FROM f JOIN px ON f.date = px.date AND f.code = px.code),
    ranked AS (
        SELECT date, fwd, NTILE(10) OVER (PARTITION BY date ORDER BY v) AS d
        FROM base WHERE fwd IS NOT NULL)
    SELECT date, d, AVG(fwd) AS ret FROM ranked
    GROUP BY date, d ORDER BY date, d
    """
    df = con.execute(q).df()
    con.execute("DETACH maindb")
    con.close()
    means = [round(float(df[df["d"] == i]["ret"].mean()) * 1e4, 1)
             for i in range(1, 11)]
    mono = round(float(pd.Series(means).corr(
        pd.Series(range(1, 11)), method="spearman")), 3)
    return {"bp": means, "mono": mono,
            "spread_bp": round(means[9] - means[0], 1),
            "n_days": int(df["date"].nunique())}


def factor_residual_ic(name: str, horizon: int,
                       start: str, end: str) -> dict | None:
    """正交化增量（§1.3 增量信息 + §8.1 冗余度排序）：因子值对核心
    生产因子（size/amihud_20，剔除自身）逐日截面 OLS 取残差后的
    rank IC——衡量对现有组合的边际贡献，而非绝对预测力。"""
    cores = [c for c in ["size", "amihud_20"] if c != name]
    parts = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    if not parts or not cores:
        return None
    plist = [str(p) for p in parts]
    ctes, joins, cols = [], [], []
    for i, c in enumerate(cores):
        cps = sorted((FACTOR_DIR / c).glob("part-*.parquet"))
        if not cps:
            continue
        cl = [str(p) for p in cps]
        ctes.append(
            f"c{i} AS (SELECT date, code, value AS cv{i} FROM read_parquet({cl})"
            f" WHERE date BETWEEN DATE '{start}' AND DATE '{end}')")
        joins.append(
            f"LEFT JOIN c{i} ON f.date = c{i}.date AND f.code = c{i}.code")
        cols.append(f"cv{i}")
    if not cols:
        return None
    sel = ", ".join(f"c{i}.cv{i}" for i in range(len(cols)))
    con = duckdb.connect()
    con.execute(f"ATTACH '{DB}' AS maindb (READ_ONLY)")
    q = f"""
    WITH f AS (
        SELECT date, code, value AS v FROM read_parquet({plist})
        WHERE date BETWEEN DATE '{start}' AND DATE '{end}'),
    px AS (SELECT date, code, close * adj_factor AS c FROM maindb.kline_daily),
    {", ".join(ctes)},
    b AS (
        SELECT f.date, f.code, f.v, {sel},
               LEAD(px.c, {horizon}) OVER (PARTITION BY f.code ORDER BY f.date)
                   / px.c - 1 AS fwd
        FROM f JOIN px ON f.date = px.date AND f.code = px.code
        {" ".join(joins)})
    SELECT date, v, {", ".join(cols)}, fwd FROM b
    WHERE fwd IS NOT NULL AND v IS NOT NULL
    """
    df = con.execute(q).df()
    con.execute("DETACH maindb")
    con.close()
    rows = []
    for _, g in df.groupby("date", sort=True):
        gg = g.dropna(subset=cols + ["fwd"])
        if len(gg) < 200:
            continue
        X = np.column_stack(
            [np.ones(len(gg))] + [gg[c].to_numpy() for c in cols])
        y = gg["v"].to_numpy()
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        # 关键：残差 Series 必须继承 gg.index，否则与 fwd_rank 按索引
        # 错位对齐（pandas corr 自动 align），结果随行序随机
        resid_rank = pd.Series(y - X @ beta, index=gg.index).rank()
        fwd_rank = gg["fwd"].rank()
        rows.append({"resid": float(resid_rank.corr(fwd_rank)),
                     "raw": float(gg["v"].rank().corr(fwd_rank))})
    if not rows:
        return None
    rd = pd.DataFrame(rows)
    return {
        "raw_ic": round(float(rd["raw"].mean()), 4),
        "raw_icir": round(float(rd["raw"].mean() / rd["raw"].std()), 3),
        "resid_ic": round(float(rd["resid"].mean()), 4),
        "resid_icir": round(float(rd["resid"].mean() / rd["resid"].std()), 3),
        "n_days": int(len(rd)), "cores": cores,
    }


# ─────────────────────────── 因子评估 ───────────────────────────
def eval_factor(a) -> dict:
    name = a.name
    aud = audit_info(name)
    if aud is None:
        raise FileNotFoundError(f"data/lake/factor_audit/{name}.json 不存在——先跑审计")
    fdr = fdr_info(name)
    reg = registry_info(name)
    ic = aud.get("checks", {}).get("ic", {})
    cov = aud.get("checks", {}).get("coverage", {})
    dist = aud.get("checks", {}).get("distribution", {})
    t0 = time.time()
    _, yearly, extra = factor_daily_metrics(name, a.horizon, a.start, a.end)
    corr = factor_core_corr(name, a.start, a.end)
    horizons = getattr(a, "horizons", None) or DEFAULT_HORIZONS
    decay, decay_meta = factor_horizon_structure(
        name, horizons, a.start, a.end)
    decile = factor_decile(name, a.horizon, a.start, a.end)
    resid = factor_residual_ic(name, a.horizon, a.start, a.end)

    verdict = aud.get("verdict", "?")
    dirn = ic.get("direction", 1)
    flags = []
    red = verdict == "FAIL"
    q = fdr.get("q") if fdr else None
    if red:
        flags.append("PIT/结构审计 FAIL")
    if verdict == "WARN":
        flags.append("审计 WARN")
    if q is None:
        flags.append("无 FDR 记录（先跑 fdr_multitest.py）")
    elif q >= 0.25:
        flags.append(f"FDR q={q}≥0.25 统计证据弱")
    if "未登记" in reg["rationale"]:
        flags.append("经济机制未登记")
    n_neg = int((yearly["ic_mean"] * dirn < 0).sum())
    if n_neg >= 2:
        flags.append(f"{n_neg}/{len(yearly)} 年 IC 反号")
    if abs(extra["roll_icir_last"]) < 0.05:
        flags.append(f"近 12M 滚动 ICIR {extra['roll_icir_last']}（信号衰减）")
    if corr is not None and len(corr):
        redundant = corr[corr["rho"].abs() > 0.7]
        for _, w in redundant.iterrows():
            flags.append(f"与核心因子 {w['core']} 相关 {w['rho']:+.2f}"
                         "（|ρ|>0.7 冗余）")
    if resid is not None and abs(resid["raw_ic"]) > 0.03 \
            and abs(resid["resid_ic"]) < 0.5 * abs(resid["raw_ic"]):
        flags.append(f"正交化后增量微弱（残差 IC {resid['resid_ic']:+.4f}"
                     f" vs 原始 {resid['raw_ic']:+.4f}）")
    light = "🔴" if red else ("🟡" if len(flags) >= 2 else "🟢")
    return {"kind": "factor", "name": name, "light": light, "flags": flags,
            "audit": aud, "ic": ic, "cov": cov, "dist": dist, "fdr": fdr,
            "reg": reg, "yearly": yearly, "extra": extra, "corr": corr,
            "decay": decay, "decay_meta": decay_meta, "decile": decile,
            "resid": resid,
            "elapsed": round(time.time() - t0, 1), "horizon": a.horizon}


# ─────────────────────────── 模型评估 ───────────────────────────
def eval_model(a) -> dict:
    from quantlab.model import build_composite
    from quantlab.optimize.backtest import run_optimized_backtest
    from quantlab.model.backtest import COST_PER_TURNOVER

    if a.name not in MODEL_SPECS:
        raise KeyError(f"未知模型 {a.name}（支持: {', '.join(MODEL_SPECS)}）")
    factors, method = MODEL_SPECS[a.name]
    t0 = time.time()
    has_short = bool(set(factors) & {"sue_i", "hf_amihud_20"})
    score = build_composite(
        factors, universe="ashare_ex",
        method=None if method == "equal" else method,
        start=max(a.start, "2025-05-01") if has_short else a.start)
    r = run_optimized_backtest(score.copy(), n_stocks=a.n_stocks,
                               rebalance=a.rebalance, method="inverse_vol",
                               start=a.start, end=a.end)
    m, ex = r["metrics"], r["excess"]
    curve = r["curve"].copy()
    curve["date"] = pd.to_datetime(curve["date"])
    curve["year"] = curve["date"].dt.year
    curve["month"] = curve["date"].dt.month
    curve["ex_ret"] = curve["ret"] - curve["bench"]

    # 年度对照（组合/基准/超额）
    gy = curve.groupby("year")
    yearly = pd.DataFrame({
        "year": gy.size().index,
        "ret": [(1 + g["ret"]).prod() - 1 for _, g in gy],
        "bench": [(1 + g["bench"].fillna(0)).prod() - 1 for _, g in gy],
        "excess": [(1 + g["ex_ret"]).prod() - 1 for _, g in gy],
        "n_days": gy.size().values,
    }).round(4)
    # 月度收益表
    gm = curve.groupby(["year", "month"])["ret"].apply(
        lambda s: (1 + s).prod() - 1).unstack(0)
    # Top3 回撤区间（不重叠标准口径：逐次剔除已报告的峰→修复段）
    nav = curve.set_index("date")["nav"]
    dd = nav / nav.cummax() - 1
    dd2 = dd.copy()
    troughs = []
    for _ in range(3):
        if dd2.empty or dd2.min() >= 0:
            break
        tr = dd2.idxmin()
        pk = dd2.loc[:tr].idxmax()
        after = dd2.loc[tr:]
        rec = after[after >= -1e-9]
        rec_end = rec.index.min() if len(rec) else None
        troughs.append({
            "peak": f"{pk:%Y-%m-%d}", "trough": f"{tr:%Y-%m-%d}",
            "recovered": (f"{rec_end:%Y-%m-%d}"
                          if rec_end is not None else "未修复"),
            "depth": round(float(dd2.loc[tr]), 3),
            "days": int((tr - pk).days),
            "recovery_days": (int((rec_end - tr).days)
                              if rec_end is not None else None),
        })
        if rec_end is None:
            break          # 最大回撤未修复，后续区间均嵌套其中
        dd2 = dd2.drop(dd2.loc[pk:rec_end].index)
    # 补充指标
    ret = curve["ret"]
    ann_vol = float(ret.std() * np.sqrt(252))
    day_win = float((ret > 0).mean())
    ex_nav = (1 + curve["ex_ret"]).cumprod()
    ex_dd_series = ex_nav / ex_nav.cummax() - 1
    calmar = m["annual_return"] / abs(m["max_drawdown"]) \
        if m["max_drawdown"] else np.nan

    ppy = 252 / a.rebalance
    ann_turn = r["turnover_avg"] * ppy
    gross = ex["excess_annual"] + ann_turn * COST_PER_TURNOVER
    be_bp = gross / ann_turn * 1e4 if ann_turn > 0 else float("nan")
    safety = be_bp / (COST_PER_TURNOVER * 1e4) - 1

    # 成分因子证据
    comp = []
    for f in factors:
        fi = fdr_info(f)
        ri = registry_info(f)
        comp.append({
            "factor": f, "ic20": (fi or {}).get("ic20"),
            "q": (fi or {}).get("q"), "t": (fi or {}).get("t"),
            "rationale": "✅" if "未登记" not in ri["rationale"] else "❌",
        })

    flags = []
    if r["tc_avg"] is None:
        flags.append("TC 无样本")
    if safety <= 0:
        flags.append("盈亏平衡成本为负（净超额不抵换手成本）")
    if m["max_drawdown"] < -0.4:
        flags.append(f"回撤 {m['max_drawdown']*100:.0f}% 深")
    if any(c["rationale"] == "❌" for c in comp):
        flags.append("存在机制未登记的成分因子")
    if any(c["q"] is not None and c["q"] >= 0.25 for c in comp):
        flags.append("存在 FDR q≥0.25 的成分因子")
    light = "🔴" if safety <= 0 else ("🟡" if len(flags) >= 2 else "🟢")

    return {"kind": "model", "name": a.name, "light": light, "flags": flags,
            "factors": factors, "method": method, "metrics": m, "excess": ex,
            "turnover": r["turnover_avg"], "tc_avg": r["tc_avg"],
            "tc_n": r["tc_n_periods"], "be_bp": be_bp, "safety": safety,
            "ann_turn": ann_turn, "yearly": yearly, "monthly": gm,
            "troughs": troughs, "ann_vol": ann_vol, "day_win": day_win,
            "ex_mdd": float(ex_dd_series.min()), "calmar": calmar,
            "comp": comp, "elapsed": round(time.time() - t0, 1)}


# ─────────────────────────── 报告渲染 ───────────────────────────
def render_factor(r: dict, a) -> str:
    ic, cov, dist, fdr = r["ic"], r["cov"], r["dist"], r["fdr"] or {}
    reg, y, ex = r["reg"], r["yearly"], r["extra"]
    dirn = ic.get("direction", 1)
    ic20 = ic.get("ic20", ic.get("ic5"))
    icir20 = ic.get("icir20", ic.get("icir5"))
    L = [
        f"# 因子评估报告：{a.name}",
        "",
        "| 元信息 | 值 |", "|---|---|",
        f"| 评估对象 | 因子 `{a.name}`（{reg['category']}）——{reg['description'][:60]} |",
        f"| 评估区间 | {a.start} ~ {a.end}（现场重算 {ex['n_days_all']} 个 IC 交易日） |",
        f"| 数据范围 | 全市场（与审计口径一致），universe 声明：{a.universe} |",
        f"| IC horizon | {r['horizon']} 日（rank IC，后复权 LEAD 收益） |",
        f"| 标签 | {', '.join(a.tags) if a.tags else '无'} |",
        f"| 生成时间 | {datetime.now():%Y-%m-%d %H:%M} |",
        "",
        "## 1. 结论摘要",
        "",
        f"**综合判定：{r['light']}**（{'无风险标记' if not r['flags'] else '；'.join(r['flags'])}）",
        "",
        "判定依据逐项：",
        "",
        "| 检验项 | 结果 | 判定 |",
        "|---|---|---|",
        f"| PIT/结构审计 | {r['audit'].get('verdict', '?')} | "
        f"{'✅' if r['audit'].get('verdict') == 'PASS' else '❌'} |",
        f"| 全期 IC{r['horizon']}（现场） | {ex['ic_all']:+.4f} / ICIR {ex['icir_all']:+.3f} | "
        f"{'✅' if abs(ex['icir_all']) > 0.1 else '🟡'} |",
        f"| FDR q（BH+自相关折减） | {fdr.get('q', '—')}（t_adj={fdr.get('t', '—')}） | "
        f"{'✅' if fdr.get('q') is not None and fdr['q'] < 0.05 else '🟡' if fdr.get('q') is not None and fdr['q'] < 0.25 else '❌'} |",
        f"| 分组单调性 | {ex['monotonicity']} | "
        f"{'✅' if abs(ex['monotonicity']) > 0.8 else '🟡'} |",
        f"| 近 12M 滚动 ICIR | {ex['roll_icir_last']} | "
        f"{'✅' if abs(ex['roll_icir_last']) > 0.15 else '🟡'} |",
        f"| 经济机制登记 | {'✅' if '未登记' not in reg['rationale'] else '❌'} | {'✅' if '未登记' not in reg['rationale'] else '❌'} |",
        f"| 年度反号 | {int((y['ic_mean'] * dirn < 0).sum())}/{len(y)} | "
        f"{'✅' if int((y['ic_mean'] * dirn < 0).sum()) < 2 else '🟡'} |",
        "",
        "## 2. 关键指标",
        "",
        "### 2.1 预测力（表现象限）",
        "",
        "| 指标 | 审计快照 | 现场重算（本次区间） |",
        "|---|---|---|",
        f"| IC{r['horizon']} | {ic20:+.4f} | {ex['ic_all']:+.4f} |",
        f"| ICIR{r['horizon']} | {icir20:+.3f} | {ex['icir_all']:+.3f} |",
        f"| IC 胜率 | {ic.get('win5', 0):.1%} | {ex['win_all']:.1%} |",
        f"| IC5（短 horizon 对照） | {ic.get('ic5', 0):+.4f} / ICIR {ic.get('icir5', 0):+.3f} | — |",
        f"| FDR q / t_adj | {fdr.get('q', '—')} / {fdr.get('t', '—')} | ρ=0.9 折减，HLZ 阈 3.0 |",
        "",
        "### 2.2 IC 衰减与多周期结构（§1.1 衰减曲线 / §8.2 衰减动力学）",
        "",
        "| horizon(日) | IC | ICIR | 胜率 | IC/峰值 |",
        "|---|---|---|---|---|",
    ]
    dm = r["decay_meta"]
    for _, row in r["decay"].iterrows():
        L.append(f"| {int(row['h'])} | {row['ic']:+.4f} | {row['icir']:+.3f} | "
                 f"{row['win']:.0%} | {abs(row['ic']) / dm['peak_ic']:.0%} |")
    hl = (f"{dm['half_life']} 日" if dm["half_life"] is not None
          else f">{dm['max_h']} 日（测程内未衰减过半）")
    L += [
        "",
        f"- 峰值 |IC| {dm['peak_ic']:.4f} 出现在 h={dm['peak_h']}；"
        f"**IC 半衰期 = {hl}**（|IC| 跌破峰值一半的持有期，线性插值）",
        f"- 换手解读：半衰期即信号**自然换手下限**——调仓周期若远长于"
        "半衰期，预测力在等待建仓时已耗散（与 R2 交易成本维度硬连接）；",
        f"- 短长周期结构：h=1 与 h=20 的 IC 差异区分交易型（快衰减、"
        "依赖高换手）与配置型（慢衰减、低换手可持有）信号；"
        "h=1 IC 显著为正而 h=20 归零的因子，其收益被换手成本吞噬的风险最高。",
        "",
        "### 2.3 覆盖与分布",
        "",
        f"- 覆盖 {cov.get('n_days', '—')} 天，截面股票中位 "
        f"{cov.get('codes_per_day_median', '—')} / 最小 {cov.get('codes_per_day_min', '—')}；"
        f"湖内最新 {r['audit'].get('latest_date', '—')}，共 "
        f"{r['audit'].get('n_rows', 0):,} 行",
        f"- 因子值分布（绝对值 q01/q50/q99）：{dist.get('abs_q01_q50_q99', '—')}，"
        f"非零率 {dist.get('nonzero_ratio', '—')}",
        "",
        "## 3. 稳定性分析",
        "",
        "### 3.1 年度分解（含五分位组收益）",
        "",
        "| 年 | 天数 | IC | ICIR | 胜率 | Q1bp | Q2bp | Q3bp | Q4bp | Q5bp | 多空bp | 多头占比 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, row in y.iterrows():
        share = (f"{row['long_share']:.0%}"
                 if pd.notna(row["long_share"]) else "—")
        L.append(
            f"| {int(row['year'])} | {int(row['n_days'])} | {row['ic_mean']:+.4f} | "
            f"{row['icir']:+.3f} | {row['win_rate']:.0%} | "
            + " | ".join(f"{row[f'q{i}_ret']*1e4:+.0f}" for i in range(1, 6))
            + f" | {row['ls_spread']*1e4:+.0f} | {share} |")
    L += [
        "",
        f"- 分组单调性（Q1→Q5 均值与组序的 spearman）：**{ex['monotonicity']}**"
        "（|ρ|=1 完全单调；<0.8 存在中段紊乱）",
        f"- 多头占比 = Q5/(Q5−Q1)：A 股无低成本做空，占比过低时预测力集中在"
        "不可收割的空头腿（E2 检验）；负值年份（多空腿同负）显示 —",
        "",
        "### 3.2 十分位分组（§1.2 分组收益，全期，D1=因子值最低 10%）",
        "",
        "| D1 | D2 | D3 | D4 | D5 | D6 | D7 | D8 | D9 | D10 | 单调性 | 多空(D10−D1) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
    dec = r["decile"]
    L.append("| " + " | ".join(f"{v:+.0f}" for v in dec["bp"]) + " | "
             f"{dec['mono']} | {dec['spread_bp']:+.0f} |")
    L += [
        "",
        f"- 单位 bp（组均远期收益 ×10⁴），horizon={r['horizon']}，"
        f"{dec['n_days']} 个截面日；十分位单调性（10 组 spearman）"
        f"= **{dec['mono']}**——比五分位更细的头部/尾部分辨率："
        "D9→D10 的增量是组合真正买入的部分（尾部集中度），"
        "D10−D1 价差给出多空全谱系强度。",
        "",
        "### 3.3 月度 IC（月均 rank IC，时变性显式化）",
        "",
    ]
    mon = ex["monthly"]
    if len(mon):
        piv = mon.unstack("month")
        months = [int(c) for c in piv.columns]
        L.append("| 年 | " + " | ".join(f"{m}月" for m in months) + " |")
        L.append("|---" * (len(months) + 1) + "|")
        for yr, row in piv.iterrows():
            cells = " | ".join(f"{v:+.3f}" if pd.notna(v) else "—"
                               for v in row)
            L.append(f"| {int(yr)} | {cells} |")
    else:
        L.append("- 无数据")
    L += [
        "",
        "### 3.4 滚动与持续期",
        "",
        f"- 12M 滚动 ICIR 区间：[{ex['roll_icir_min']}, {ex['roll_icir_max']}]，"
        f"最新 {ex['roll_icir_last']}",
        f"- 连续同号最长段：{ex['max_run_same_sign']} 个交易日"
        "（IC 符号最长不中断持续期）",
        "",
        "### 3.5 与核心因子相关性（冗余检查，最近 250 日截面 spearman 均值）",
        "",
    ]
    if r["corr"] is not None and len(r["corr"]):
        L += ["| 核心因子 | ρ | 天数 |", "|---|---|---|"]
        for _, row in r["corr"].iterrows():
            mark = " ⚠️冗余" if abs(row["rho"]) > 0.7 else ""
            L.append(f"| {row['core']} | {row['rho']:+.3f}{mark} | "
                     f"{int(row['n_days'])} |")
    else:
        L.append("- 无数据")
    res = r["resid"]
    L += [
        "",
        "### 3.6 正交化增量（§1.3 增量信息 / §8.1 冗余度排序）",
        "",
        f"因子值对 {' + '.join(res['cores'])} 逐日截面 OLS 取残差后的 rank IC"
        "（同样本对照）：",
        "",
        "| 口径 | IC | ICIR | 样本天数 |",
        "|---|---|---|---|",
        f"| 原始（同窗样本） | {res['raw_ic']:+.4f} | {res['raw_icir']:+.3f} | "
        f"{res['n_days']} |",
        f"| 正交化残差 | **{res['resid_ic']:+.4f}** | "
        f"**{res['resid_icir']:+.3f}** | {res['n_days']} |",
        "",
        "- 残差 IC 衡量**对现有生产组合的边际贡献**（增量信息），而非绝对"
        "预测力：残差 IC 显著缩水 → 该因子与生产因子共有成分高，入库价值"
        "需用边际贡献重排（a191 去重 5 因子的同款逻辑）；残差 IC 保持"
        " → 独立信息源，组合分散价值大。",
        "",
        "## 4. 风险与合规提示",
        "",
    ]
    if r["flags"]:
        L += [f"- ⚠️ {f}" for f in r["flags"]]
    else:
        L += ["- 无风险标记"]
    L += [
        "- FDR q 基于审计时 IC 快照（非同窗重算），BH 校正偏保守；",
        "- IC 好≠组合好：组合只吃尾部，须过组合层 A/B 终审方可入生产候选；",
        "- 拥挤/容量维度未在本报告覆盖（需另跑容量测算，R4 维度）。",
        "",
        "## 5. 口径附注",
        "",
        f"- 年度 IC 为全市场 rank IC，horizon={r['horizon']}，后复权收益 "
        f"LEAD({r['horizon']})，in-memory DuckDB 只读；审计快照："
        f"{r['audit'].get('audited_at', '—')}；评估耗时 {r['elapsed']}s。",
        f"- 多周期 IC 谱 horizons={list(r['decay']['h'].astype(int))}；"
        "半衰期按 |IC| 线性插值，测程不足时报下限；",
        "- 正交化增量=逐日截面 OLS 残差（对 size+amihud_20，剔除自身）的 "
        "rank IC，衡量对生产组合的边际贡献；",
        "- 框架中未在本报告覆盖的项：因子模拟组合 / FF 回归 alpha（由"
        "组合层 A/B 终审覆盖）、构造参数敏感性（需因子重算）、拥挤与容量"
        "（R4 另行测算）、跨市场对照（R6）。",
    ]
    return "\n".join(L)


def render_model(r: dict, a) -> str:
    m, ex, y = r["metrics"], r["excess"], r["yearly"]
    monthly = r["monthly"]
    L = [
        f"# 模型评估报告：{a.name}",
        "",
        "| 元信息 | 值 |", "|---|---|",
        f"| 评估对象 | 模型 `{a.name}` = {' + '.join(r['factors'])}"
        f"（{r['method']} 合成） |",
        f"| 评估区间 | {a.start} ~ {a.end} |",
        f"| 数据范围 | universe={a.universe}，n={a.n_stocks}，"
        f"rebalance={a.rebalance}，inverse_vol，基准中证1000 |",
        f"| 标签 | {', '.join(a.tags) if a.tags else '无'} |",
        f"| 生成时间 | {datetime.now():%Y-%m-%d %H:%M} |",
        "",
        "## 1. 结论摘要",
        "",
        f"**综合判定：{r['light']}**（{'无风险标记' if not r['flags'] else '；'.join(r['flags'])}）",
        "",
        f"年化 {m['annual_return']:.1%} / 夏普 {m['sharpe']:.2f} / "
        f"回撤 {m['max_drawdown']:.1%}；超额 {ex['excess_annual']:.1%} / "
        f"IR {ex['information_ratio']:.2f}；盈亏平衡 {r['be_bp']:.0f}bp"
        f"（安全边际 {r['safety']:+.0%}）；TC {r['tc_avg'] or '—'}。",
        "",
        "## 2. 关键指标",
        "",
        "### 2.1 收益与风险（表现/风险象限）",
        "",
        "| 指标 | 值 |", "|---|---|",
        f"| 年化收益 | {m['annual_return']:.1%} |",
        f"| 年化波动 | {r['ann_vol']:.1%} |",
        f"| 夏普 | {m['sharpe']:.2f} |",
        f"| 最大回撤 | {m['max_drawdown']:.1%} |",
        f"| Calmar（年化/|回撤|） | {r['calmar']:.2f} |",
        f"| 日频胜率 | {r['day_win']:.1%} |",
        f"| 超额年化 | {ex['excess_annual']:.1%} |",
        f"| 超额回撤 | {r['ex_mdd']:.1%} |",
        f"| 信息比率 IR | {ex['information_ratio']:.2f} |",
        "",
        "### 2.2 可用性与转移效率",
        "",
        "| 指标 | 值 | 说明 |", "|---|---|---|",
        f"| 期换手 / 年换手 | {r['turnover']:.0%} / {r['ann_turn']:.0%} | L1/2 口径 |",
        f"| 盈亏平衡成本 | {r['be_bp']:.0f}bp | 毛超额/年换手 |",
        f"| 成本安全边际 | {r['safety']:+.0%} | vs 引擎 35bp |",
        f"| TC（corr score↔主动权重） | {r['tc_avg']}（{r['tc_n']} 期） | "
        "Grinold-Kahn：IR=IC·√BR·TC 的转移项 |",
        "",
        "### 2.3 成分因子证据（真伪闸门在模型层的传导）",
        "",
        "| 成分因子 | IC20 | FDR q | t_adj | 机制登记 |", "|---|---|---|---|---|",
    ]
    for c in r["comp"]:
        L.append(f"| {c['factor']} | {c['ic20'] if c['ic20'] is not None else '—'} | "
                 f"{c['q'] if c['q'] is not None else '—'} | "
                 f"{c['t'] if c['t'] is not None else '—'} | {c['rationale']} |")
    L += [
        "",
        "## 3. 稳定性分析",
        "",
        "### 3.1 年度分解（组合 / 基准 / 超额对照）",
        "",
        "| 年 | 交易日 | 组合 | 基准 | 超额 |", "|---|---|---|---|---|",
    ]
    for _, row in y.iterrows():
        L.append(f"| {int(row['year'])} | {int(row['n_days'])} | "
                 f"{row['ret']:+.1%} | {row['bench']:+.1%} | "
                 f"{row['excess']:+.1%} |")
    L += [
        "",
        "### 3.2 月度收益（%）",
        "",
    ]
    mrows = monthly.copy()
    hdr = "| 月 | " + " | ".join(str(int(c)) for c in mrows.columns) + " |"
    L += [hdr, "|---" * (len(mrows.columns) + 1) + "|"]
    for mi, row in mrows.iterrows():
        cells = " | ".join(f"{v*100:+.1f}" if pd.notna(v) else "—" for v in row)
        L.append(f"| {int(mi)} | {cells} |")
    L += [
        "",
        "### 3.3 Top 3 回撤区间",
        "",
        "| 峰值日 | 谷底日 | 修复日 | 深度 | 下跌天数 | 修复天数 |",
        "|---|---|---|---|---|---|",
    ]
    for t in r["troughs"]:
        L.append(f"| {t['peak']} | {t['trough']} | {t['recovered']} | "
                 f"{t['depth']:.1%} | {t['days']} | "
                 f"{t['recovery_days'] if t['recovery_days'] else '—'} |")
    L += [
        "",
        "## 4. 风险与合规提示",
        "",
    ]
    if r["flags"]:
        L += [f"- ⚠️ {f}" for f in r["flags"]]
    else:
        L += ["- 无风险标记"]
    L += [
        "- 盈亏平衡衡量『成本翻倍也死不了』，不代表大资金可复制——真实约束"
        "是冲击成本非线性（容量维度另行评估，PROD 参考容量 1.59 亿）；",
        "- TC 为等权基准近似下的转移效率，样本期为窗口内调仓期；",
        "- 入生产候选须过组合层 A/B 终审 + 双闸门（2026-12 下次评审）。",
        "",
        "## 5. 口径附注",
        "",
        f"- 新口径（as-of 根治后）回测；模型组合定义 run_eval.py MODEL_SPECS；"
        f"评估耗时 {r['elapsed']}s；引擎 quantlab/optimize/backtest.py。",
    ]
    return "\n".join(L)


# ─────────────────────────── 主流程 ───────────────────────────
def main() -> None:
    p = argparse.ArgumentParser(description="因子/模型系统化评估 v2")
    p.add_argument("--type", required=True, choices=["factor", "model"])
    p.add_argument("--name", required=True)
    p.add_argument("--start", default="2022-01-01")
    p.add_argument("--end", default="2026-09-11")
    p.add_argument("--universe", default="ashare_ex")
    p.add_argument("--tags", default="")
    p.add_argument("--horizon", type=int, default=20)
    p.add_argument("--horizons", default="",
                   help="多周期 IC 谱，逗号分隔，默认 1,3,5,10,20,40,60")
    p.add_argument("--n-stocks", type=int, default=100)
    p.add_argument("--rebalance", type=int, default=20)
    a = p.parse_args()
    a.tags = [t.strip() for t in a.tags.split(",") if t.strip()]
    a.horizons = ([int(x) for x in a.horizons.split(",")
                   if x.strip()] or DEFAULT_HORIZONS)

    print(f"[eval] {a.type} {a.name} {a.start}~{a.end} ...")
    r = eval_factor(a) if a.type == "factor" else eval_model(a)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now()
    fname = f"{a.type}_{a.name}_{ts:%Y%m%d}_{ts:%H%M}.md"
    fpath = OUT_DIR / fname
    body = render_factor(r, a) if a.type == "factor" else render_model(r, a)
    fpath.write_text(body, encoding="utf-8")

    idx = load_index()
    entry = {
        "id": f"{a.type}:{a.name}:{ts:%Y%m%d%H%M}",
        "type": a.type, "name": a.name,
        "date": f"{ts:%Y-%m-%d}",
        "generated_at": ts.isoformat(timespec="seconds"),
        "window": f"{a.start}~{a.end}", "universe": a.universe,
        "tags": a.tags, "light": r["light"], "flags": r["flags"],
        "file": fname, "template": "v3",
    }
    if a.type == "factor":
        entry["summary"] = (f"IC{r['horizon']} {r['extra']['ic_all']:+.4f} "
                            f"q={r['fdr'].get('q') if r['fdr'] else '—'} "
                            f"mono={r['extra']['monotonicity']} "
                            f"hl={r['decay_meta']['half_life']} "
                            f"PIT={r['audit'].get('verdict')}")
    else:
        m, ex = r["metrics"], r["excess"]
        entry["summary"] = (f"annual {m['annual_return']:.1%} "
                           f"sharpe {m['sharpe']:.2f} "
                           f"excess {ex['excess_annual']:.1%} "
                           f"TC {r['tc_avg']}")
    idx.append(entry)
    save_index(idx)

    print(f"[done] {r['light']}  {entry['summary']}")
    for f in r["flags"]:
        print(f"  ⚠️ {f}")
    print(f"[report] {fpath}")
    print(f"[index] {INDEX}（共 {len(idx)} 条）")


if __name__ == "__main__":
    main()
