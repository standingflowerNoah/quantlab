# -*- coding: utf-8 -*-
"""全因子单因子看板批量生成器（湖模式 dashboard-lake-v1）
======================================================================
为因子池全部因子生成七区单因子看板 docs/factor_center_{name}.html，
使总览看板（build_universe_dashboard.py）的跳转链接全部生效。

与 build_dashboard.py（dashboard-v3，依赖主库 DuckDB ATTACH）互补：
本脚本为**湖模式**——数据源全只读、零主库依赖，主库被流水线写锁
占用时也可批量生成。区块口径差异（诚实标注在页内）：
  IC 谱 = L0 h=1/5/20/60/120 五点（v3 为 1~120 逐点）
  分组收益 = L0 q*_ret20（h=20 摊薄）五分位 + 十分位（mirror adj close LEAD 20）；
  正交化残差 IC = 因子值对 size/amihud_20 逐日 OLS 残差 rank IC（湖内自算，v3 同口径）
  风格暴露 = 因子湖 6 风格代表因子截面 spearman（湖内现算）
  正交化残差 IC 未含（需逐股价格 OLS，主库空闲时可用 v3 覆盖重跑）

用法：
  python scripts/factor_eval/build_all_center_lake.py            # 全部
  python scripts/factor_eval/build_all_center_lake.py --only a,b # 指定因子
  python scripts/factor_eval/build_all_center_lake.py --retry    # 只跑上次失败

产出：docs/factor_center_{name}.html × N + index.json 登记 + 失败清单
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from batch_metrics import (  # noqa: E402
    AUDIT_DIR, load_audit, load_fdr, load_latest_reports, load_rationales,
)
FACTOR_DIR = ROOT / "data" / "lake" / "factor"
DOCS = ROOT / "docs"
INDEX = ROOT / "reports" / "factor_eval" / "index.json"
FAIL_LOG = HERE / "_lake_board_failures.json"

STYLES = {"size": "市值", "bp": "账面市值比（价值）", "momentum_20": "动量（20日）",
          "volatility_20": "波动率", "turnover": "换手率", "reversal_5": "短期反转"}
HORIZONS = (1, 5, 20, 60, 120)


# ───────────────────────────── 预加载层 ─────────────────────────────
def preload(pool: str = "ashare_ex") -> dict:
    """全库数据一次加载（全只读）"""
    from quantlab.factor.metric_store import read_l0
    print("[preload] L0 全量读取…")
    l0 = read_l0(pool)
    l0["date"] = pd.to_datetime(l0["date"])
    l0 = l0.sort_values(["factor", "date"])

    print("[preload] L1 滚动指标…")
    l1_files = sorted((ROOT / "data" / "lake" / "factor_metric_rolling"
                       / pool).glob("*.parquet"))
    l1 = (pd.concat([pd.read_parquet(p) for p in l1_files])
          if l1_files else pd.DataFrame())
    if not l1.empty:
        l1["date"] = pd.to_datetime(l1["date"])

    print("[preload] 核心因子相关 + 拥挤度…")
    corr_files = sorted((ROOT / "data" / "lake" / "factor_corr_daily")
                        .glob("*.parquet"))
    corr = (pd.concat([pd.read_parquet(p) for p in corr_files],
                      ignore_index=True) if corr_files else pd.DataFrame())
    if not corr.empty:
        corr["date"] = pd.to_datetime(corr["date"])
    crowd_json = json.loads((ROOT / "data" / "lake" / "crowding"
                             / "latest.json").read_text(encoding="utf-8"))
    crowd = crowd_json.get("latest", {})

    print("[preload] 审计 / FDR / 红绿灯 / registry…")
    audits = {p.stem: load_audit(p.stem) for p in AUDIT_DIR.glob("*.json")}
    fdrs = load_fdr()
    reports = load_latest_reports()
    rationales = load_rationales()
    reg_names = set(rationales) if isinstance(rationales, dict) else set()

    print("[preload] 风格代表因子矩阵（全史）…")
    style_mats: dict[str, pd.DataFrame] = {}
    for s in STYLES:
        style_mats[s] = _pivot_factor(s)

    print("[preload] L0 扩展湖（十分位+残差 IC 预计算列）…")
    ext_files = sorted((ROOT / "data" / "lake" / "factor_metric_ext"
                        / pool).glob("part-*.parquet"))
    ext = (pd.concat([pd.read_parquet(p) for p in ext_files],
                     ignore_index=True) if ext_files else pd.DataFrame())
    if not ext.empty:
        ext["date"] = pd.to_datetime(ext["date"])
        ext = ext.sort_values(["factor", "date"])

    names = sorted(p.stem for p in AUDIT_DIR.glob("*.json"))
    print(f"[preload] 完成：{len(names)} 因子｜L0 {l0['date'].max():%Y-%m-%d}")
    return {"l0": l0, "l1": l1, "corr": corr, "crowd": crowd,
            "audits": audits, "fdrs": fdrs, "reports": reports,
            "rationales": rationales, "reg_names": reg_names,
            "styles": style_mats, "names": names,
            "ext": ext,
            "l0_end": l0["date"].max()}


def _value_col(p: Path) -> str:
    """因子湖文件值列名：普通因子 value，ML 分数因子 score（多一列 schema）"""
    import pyarrow.parquet as pq
    names = pq.ParquetFile(p).schema_arrow.names
    return "score" if ("value" not in names and "score" in names) else "value"


def _pivot_factor(name: str) -> pd.DataFrame:
    """因子湖单因子 → 宽表（index=date, columns=code）"""
    fs = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    if not fs:
        return pd.DataFrame()
    vc = _value_col(fs[0])
    df = pd.concat([pd.read_parquet(p, columns=["date", "code", vc])
                    for p in fs], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot_table(index="date", columns="code", values=vc,
                          aggfunc="last")


def _latest_top(name: str, k: int = 10) -> dict:
    """因子最新截面 Top10（按因子值降序）"""
    fs = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    if not fs:
        return {"date": "", "top": []}
    last = pd.read_parquet(fs[-1], columns=["date"])
    d = pd.to_datetime(last["date"]).max()
    # ML 分数因子湖文件值列名是 score（还多一列 schema），普通因子是 value
    val_col = _value_col(fs[-1])
    df = pd.read_parquet(fs[-1], columns=["date", "code", val_col])
    df = df[pd.to_datetime(df["date"]) == d].nlargest(k, val_col)
    return {"date": f"{d:%Y-%m-%d}",
            "top": [{"code": r.code,
                     "value": round(float(getattr(r, val_col)), 4)}
                    for r in df.itertuples()]}


# ─────────────────────────── 单因子计算层 ───────────────────────────
def _daily_stats(s: pd.Series) -> dict:
    s = s.dropna()
    if len(s) < 5 or s.std() == 0:
        return {}
    acf1 = float(s.autocorr(1)) if len(s) > 2 else 0.0
    rho = acf1 if not math.isnan(acf1) else 0.0
    n = len(s)
    n_eff = n * (1 - rho) / (1 + rho)
    icir = float(s.mean() / s.std())
    return {"mean": round(float(s.mean()), 4), "std": round(float(s.std()), 4),
            "icir": round(icir, 3), "n": n, "rho": round(rho, 3),
            "t_adj": round(icir * math.sqrt(max(n_eff, 1)), 2),
            "win": round(float((s > 0).mean()), 3),
            "skew": round(float(s.skew()), 3), "kurt": round(float(s.kurt()), 3)}


def _half_life(spec: dict) -> tuple[str, str]:
    """五点谱拟合半衰期（|IC| 指数衰减），非单调/拟合失败返回 (">max_h", note)"""
    hs = np.array(HORIZONS, dtype=float)
    ics = np.array([abs(spec[h]["rank"]["mean"]) if h in spec and spec[h]
                    else np.nan for h in HORIZONS])
    ok = ~np.isnan(ics) & (ics > 1e-4)
    if ok.sum() < 3:
        return f">{int(hs[-1])}", "谱点不足，半衰期未估计"
    # 峰值在 h=1 → 常规衰减拟合；否则峰值持有期起拟合
    peak = int(hs[np.nanargmax(ics)])
    if peak >= 40:
        return f">{peak}", f"峰值 |IC| 在 h={peak}——慢周期配置型信号"
    x, y = np.log(ics[ok]), hs[ok]
    b = np.polyfit(x, y, 1)[0]
    if b >= 0:
        return f">{int(hs[-1])}", "测程内 |IC| 未随 h 衰减"
    tau = -1.0 / b
    return (str(int(round(tau * math.log(2)))) if tau < 500
            else f">{int(hs[-1])}"), ""


def compute_one(name: str, P: dict) -> dict:
    l0f = P["l0"][P["l0"]["factor"] == name]
    if l0f.empty:
        raise ValueError("L0 无该因子数据")
    a = P["audits"].get(name) or {}
    f = P["fdrs"].get(name) or {}
    rep = P["reports"].get(name) or {}
    dirn = (a.get("checks", {}).get("ic", {}).get("direction") or 1)

    ic5 = l0f.set_index("date")["rank_ic5"]
    ic20 = l0f.set_index("date")["rank_ic20"]
    s5 = _daily_stats(ic5)
    s20 = _daily_stats(ic20)
    # 多周期谱 + Pearson
    spec = {}
    for h in HORIZONS:
        sr = _daily_stats(l0f.set_index("date")[f"rank_ic{h}"])
        pr = _daily_stats(l0f.set_index("date")[f"pearson_ic{h}"])
        if sr:
            spec[h] = {"rank": sr, "pear": pr}
    half, half_note = _half_life(spec)

    # 近 1 年窗口动态（近 244 个交易日，与总览看板同口径）
    y1 = l0f.tail(244)
    s5_1y = _daily_stats(y1.set_index("date")["rank_ic5"])
    s20_1y = _daily_stats(y1.set_index("date")["rank_ic20"])

    # L1 滚动 ICIR
    l1f = P["l1"][P["l1"]["factor"] == name] if not P["l1"].empty else pd.DataFrame()
    roll = None
    if not l1f.empty:
        r = l1f[["date", "rank_icir20_w20", "rank_icir20_w60"]].dropna(
            subset=["rank_icir20_w20", "rank_icir20_w60"], how="all").tail(500)
        if len(r) >= 10:
            roll = {"dates": [f"{d:%Y-%m-%d}" for d in r["date"]],
                    "w20": [round(float(v), 3) if pd.notna(v) else None
                            for v in r["rank_icir20_w20"]],
                    "w60": [round(float(v), 3) if pd.notna(v) else None
                            for v in r["rank_icir20_w60"]]}

    # 因子宽表：风格暴露 / 十分位 / 残差 IC 共用
    fmat = _pivot_factor(name)

    # 风格暴露（全史逐日 spearman = rank-Pearson，向量化）
    style_exp = {}
    try:
        if not fmat.empty:
            fr = fmat.rank(axis=1)
            for sname, smat in P["styles"].items():
                if smat.empty:
                    continue
                common = fr.index.intersection(smat.index)
                if len(common) < 60:
                    continue
                a1, a2 = fr.loc[common], smat.loc[common].rank(axis=1)
                mask = a1.notna() & a2.notna()
                n1, n2 = a1.where(mask), a2.where(mask)
                cov = (n1 * n2).mean(axis=1) - n1.mean(axis=1) * n2.mean(axis=1)
                sd = n1.std(axis=1) * n2.std(axis=1)
                rho = (cov / sd).replace([np.inf, -np.inf], np.nan).dropna()
                if len(rho) < 60:
                    continue
                yearly = rho.groupby(rho.index.year).mean().round(3)
                style_exp[sname] = {"all": round(float(rho.mean()), 3),
                                    "n_days": int(len(rho)),
                                    "yearly": {int(y): float(v)
                                               for y, v in yearly.items()}}
    except Exception:
        style_exp = {}

    # 十分位 + 正交化残差 IC（读 L0 扩展湖预计算列，轻聚合——重截面计算
    # 已由 backfill_metric_ext.py 一次性落库，本处只做均值/ICIR/单调性）
    decile, resid = None, None
    ef = (P["ext"][P["ext"]["factor"] == name]
          if not P["ext"].empty else pd.DataFrame())
    if not ef.empty:
        e = ef.set_index("date").sort_index()
        dcols = [f"dec{i}_ret20" for i in range(1, 11)]
        if all(c in e.columns for c in dcols):
            bp = [round(float(e[c].mean()) * 1e4, 1) for c in dcols]
            if all(pd.notna(v) for v in bp):
                mono = pd.Series(bp).corr(pd.Series(range(1, 11)),
                                          method="spearman")
                decile = {"bp": bp,
                          "mono": (round(float(mono), 3)
                                   if pd.notna(mono) else None),
                          "spread_bp": round(bp[9] - bp[0], 1),
                          "n_days": int(e[dcols[0]].notna().sum())}
        if "resid_ic20" in e.columns:
            ri = e["resid_ic20"].dropna()
            rr = e["resid_raw_ic20"].dropna()
            if len(ri) >= 30:
                cores = [c for c in ("size", "amihud_20")
                         if c != name and (FACTOR_DIR / c).exists()]
                s_r, s_w = rr.std(), ri.std()
                resid = {"raw_ic": round(float(rr.mean()), 4),
                         "raw_icir": (round(float(rr.mean() / s_r), 3)
                                      if s_r else None),
                         "resid_ic": round(float(ri.mean()), 4),
                         "resid_icir": (round(float(ri.mean() / s_w), 3)
                                        if s_w else None),
                         "n_days": int(len(ri)), "cores": cores}

    # 分组分析（L0 q*_ret20，h=20 摊薄调仓口径）
    g = l0f.set_index("date")
    qcols = {f"q{i}_ret20": g[f"q{i}_ret20"] / 20.0 for i in range(1, 6)}
    ls20 = (g["q5_ret20"] - g["q1_ret20"]) / 20.0
    qcols["LS"] = ls20

    def gstats(s: pd.Series) -> dict:
        s = s.dropna()
        if len(s) < 30:
            return {}
        nav = (1 + s).cumprod()
        mon = s.groupby([s.index.year, s.index.month]).apply(
            lambda x: (1 + x).prod() - 1)
        return {"ann": round(float(s.mean() * 252), 4),
                "vol": round(float(s.std() * math.sqrt(252)), 4),
                "sharpe": round(float(s.mean() * 252)
                                / (s.std() * math.sqrt(252)), 2)
                if s.std() else None,
                "mdd": round(float((nav / nav.cummax() - 1).min()), 3),
                "monwin": round(float((mon > 0).mean()), 3),
                "bp": round(float(s.mean()) * 1e4 * 20, 1)}
    groups = {k: gstats(v) for k, v in qcols.items()}
    navs, nav_dates = {}, []
    for k, v in qcols.items():
        d = v.dropna()
        navs[k] = [round(float(x), 4) for x in (1 + d).cumprod()]
        if k == "Q1":
            nav_dates = [f"{x:%Y-%m-%d}" for x in d.index]
    # 年度分解（h=20 口径）
    yearly = []
    if not g.empty:
        yy = g[["rank_ic20", "q1_ret20", "q5_ret20"]].copy()
        yy["ls20"] = g["q5_ret20"] - g["q1_ret20"]
        for y, sub in yy.groupby(yy.index.year):
            ic = sub["rank_ic20"].dropna()
            row = {"year": int(y), "n": int(len(ic)),
                   "ic": round(float(ic.mean()), 4) if len(ic) else None,
                   "icir": (round(float(ic.mean() / ic.std()), 2)
                            if len(ic) > 2 and ic.std() else None),
                   "q1": round(float(sub["q1_ret20"].mean()), 5),
                   "q5": round(float(sub["q5_ret20"].mean()), 5),
                   "ls": round(float(sub["ls20"].mean()), 5)}
            yearly.append(row)
    n_neg = sum(1 for r in yearly if r["ic"] is not None and r["ic"] * dirn < 0)

    # 信息增量：与核心因子相关（corr 湖）
    cf = P["corr"][P["corr"]["factor"] == name]
    corr_rows = []
    if not cf.empty:
        recent = cf[cf["date"] >= cf["date"].max() - pd.Timedelta(days=250)]
        for core, sub in recent.groupby("core_factor"):
            rho = sub["rho"].mean()
            corr_rows.append({"core": core, "rho": round(float(rho), 3),
                              "n": int(len(sub)),
                              "redundant": bool(abs(rho) > 0.7)})
        corr_rows.sort(key=lambda x: -abs(x["rho"]))
    crowd = P["crowd"].get(name)

    # 红绿灯（湖可用子集，规则与 run_eval 同源）
    flags = []
    verdict = a.get("verdict", "未审计") if a else "未审计"
    red = verdict == "FAIL"
    if red:
        flags.append("PIT/结构审计 FAIL")
    if not a:
        flags.append("未审计（无 audit JSON）")
    q = f.get("q")
    if q is None:
        flags.append("无 FDR 记录")
    elif q >= 0.25:
        flags.append(f"FDR q={q} 统计证据弱")
    if name not in P["reg_names"]:
        flags.append("经济机制未登记")
    if n_neg >= 2:
        flags.append(f"{n_neg}/{len(yearly)} 年 IC20 反号")
    if roll and roll["w60"] and roll["w60"][-1] is not None \
            and abs(roll["w60"][-1]) < 0.05:
        flags.append(f"近端滚动 ICIR20(60d) {roll['w60'][-1]}（信号衰减）")
    reds = [c for c in corr_rows if c["redundant"]]
    for c in reds:
        flags.append(f"与核心因子 {c['core']} 相关 {c['rho']:+.2f}（冗余）")
    if resid is not None and abs(resid["raw_ic"]) > 0.03 \
            and abs(resid["resid_ic"]) < 0.5 * abs(resid["raw_ic"]):
        flags.append(f"正交化后增量微弱（残差 IC {resid['resid_ic']:+.4f}"
                     f" vs 原始 {resid['raw_ic']:+.4f}）")
    light = "🔴" if red else ("🟡" if len(flags) >= 2 else "🟢")

    latest = _latest_top(name)
    return {"name": name, "light": light, "flags": flags, "verdict": verdict,
            "q": q, "t_lib": f.get("t"), "dirn": dirn,
            "s5": s5, "s20": s20, "s5_1y": s5_1y, "s20_1y": s20_1y,
            "spec": spec, "half": half, "half_note": half_note,
            "roll": roll, "style_exp": style_exp,
            "decile": decile, "resid": resid,
            "groups": groups, "navs": navs, "nav_dates": nav_dates,
            "yearly": yearly, "n_neg": n_neg,
            "corr_rows": corr_rows, "crowd": crowd,
            "latest": latest,
            "ic5_series": [round(float(v), 4) for v in ic5.dropna().values],
            "ic5_acf": [round(float(ic5.autocorr(k)), 3)
                        for k in range(1, 11)],
            "monthly": _monthly(ic5),
            "rationale": (P["rationales"].get(name) if isinstance(
                P["rationales"], dict) else None) or "未登记"}


def _monthly(s: pd.Series) -> dict:
    s = s.dropna()
    m = s.groupby([s.index.year, s.index.month]).mean().unstack()
    return {"years": [int(i) for i in m.index],
            "months": [int(c) for c in m.columns],
            "values": [[None if pd.isna(v) else round(float(v), 4)
                        for v in row] for row in m.values]}


# ───────────────────────────── HTML 模板 ─────────────────────────────
def build_html(D: dict, X: dict) -> str:
    payload = json.dumps(D, ensure_ascii=False).replace("</", "<\\/")
    TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>因子看板 · __NAME__（湖模式）</title>
<style>
:root{
  --bg:#1a1a1a; --panel:#232323; --panel2:#2a2a2a; --line:#3a3a3a;
  --txt:#e8e8e8; --sub:#9a9a9a; --gold:#e6b455; --red:#e05d5d;
  --green:#5dc98e; --blue:#5da9e0; --purple:#b48ee6; --cyan:#55c4c4;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);
  font-family:"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif;
  font-size:14px;line-height:1.6;padding-bottom:60px}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px}
header{padding:26px 0 10px;border-bottom:1px solid var(--line)}
h1{font-size:21px;font-weight:700}
h1 .badge{font-size:11px;background:var(--gold);color:#1a1a1a;border-radius:4px;
  padding:2px 8px;vertical-align:3px;margin-left:10px;font-weight:700}
.meta{color:var(--sub);font-size:12px;margin-top:6px}
h2{font-size:16px;font-weight:700;margin:34px 0 6px}
.desc{color:var(--sub);font-size:12px;margin-bottom:14px}
.grid{display:grid;gap:12px}
.g2{grid-template-columns:1fr 1fr}.g4{grid-template-columns:repeat(4,1fr)}
@media(max-width:900px){.g2,.g4{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
.metric{background:var(--panel2);border-radius:8px;padding:10px 12px}
.metric .k{color:var(--sub);font-size:11px;letter-spacing:.4px}
.metric .v{font-size:20px;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums}
.metric .s{color:var(--sub);font-size:11px}
table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
th{color:var(--sub);text-align:right;font-weight:500;padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{text-align:right;padding:6px 8px;border-bottom:1px solid #2e2e2e;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
.pos{color:var(--red)} .neg{color:var(--green)}
.chip{display:inline-block;font-size:11px;border-radius:10px;padding:1px 10px;
  border:1px solid var(--line);color:var(--sub);margin:2px}
.ok{color:var(--green);font-weight:700}.warn{color:var(--gold);font-weight:700}
.light{font-size:34px;line-height:1}
svg text{font-family:"PingFang SC","Microsoft YaHei",sans-serif}
.note{color:var(--sub);font-size:11.5px;margin-top:8px}
.heatmap td,.heatmap th{padding:5px 6px;text-align:center;font-size:11.5px}
a{color:var(--blue);text-decoration:none}
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--sub);font-size:11.5px}
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>因子看板 · __NAME__<span class="badge">湖模式 v1</span></h1>
  <div class="meta">__DESC__｜__CATEGORY__｜窗口 __START__ ~ __END__（L0 全史）｜L0 数据更新至 __L0_END__｜生成 __GENERATED__｜<a href="factor_universe.html">← 返回总览看板</a></div>
</header>

<h2>概览指标</h2>
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px">
    <div><b style="font-size:16px">__NAME__</b>
      <span class="chip">方向 __DIRN__</span><span class="chip">机制登记：__RATIONALE__</span></div>
    <div style="color:var(--sub);font-size:11.5px">因子池 __NPOOL__ 个（已审计 __NAUDITED__）</div>
  </div>
  <div class="grid g4" style="margin-top:12px">
    <div class="metric"><div class="k">Rank IC5（全史）</div><div class="v __IC5_CLS__">__IC5__</div></div>
    <div class="metric"><div class="k">ICIR5（全史）</div><div class="v">__ICIR5__</div></div>
    <div class="metric"><div class="k">Rank IC20 / ICIR20（全史）</div><div class="v">__IC20__ / __ICIR20__</div></div>
    <div class="metric"><div class="k">近 1 年 ICIR20</div><div class="v">__Y1_ICIR20__</div><div class="s">近 1 年 __Y1_DAYS__ 个成熟日</div></div>
  </div>
  <div class="grid g4" style="margin-top:8px">
    <div class="metric"><div class="k">FDR q（BH + 自相关折减，库级 h=20）</div><div class="v">__FDR_Q__</div><div class="s">t_adj(库级) __T_LIB__</div></div>
    <div class="metric"><div class="k">PIT 审计</div><div class="v" style="color:__PIT_CLS__">__PIT__</div><div class="s">时点信息合规</div></div>
    <div class="metric"><div class="k">IC5 半衰期（五点谱拟合）</div><div class="v">__HALF__</div><div class="s">__HALF_NOTE__</div></div>
    <div class="metric"><div class="k">IC5 胜率（全史）</div><div class="v">__WIN5__</div><div class="s">__IC5_N__ 个交易日</div></div>
  </div>
</div>

<h2>结论摘要</h2>
<div class="card">
  <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap">
    <div><div class="light">__LIGHT__</div><div style="color:var(--sub);font-size:11px">综合判定（初筛）</div></div>
    <div style="flex:1;min-width:280px"><b>__VERDICT_HEAD__</b><br>
      <span style="color:var(--sub);font-size:12px">__VERDICT_SUMMARY__。红绿灯仅初筛，入生产候选须组合层 A/B 终审。</span></div>
  </div>
__VERDICT_TABLE__
</div>

<h2>IC 分析</h2>
<div class="desc">主口径 IC5（5 日 rank IC）｜多周期谱 h=1/5/20/60/120（湖口径五点）｜分布 / 自相关 / 月度时变</div>
<div class="grid g2">
  <div class="card"><b>多周期 IC / ICIR 谱（Rank，h = 1 ~ 120 五点）</b>
    <div id="chart_decay"></div><div class="note">__DECAY_NOTE__</div></div>
  <div class="card"><b>IC5 分布（skew = __SKEW__，kurt = __KURT__）</b>
    <div id="chart_hist"></div><div class="note">__HIST_NOTE__</div></div>
  <div class="card"><b>IC5 自相关（lag 1 ~ 10）</b>
    <div id="chart_acf"></div><div class="note">__ACF_NOTE__</div></div>
  <div class="card"><b>月度 IC5 热力图</b>
    <div style="overflow-x:auto"><table class="heatmap" id="tbl_monthly"></table></div>
    <div class="note">IC 深负/深正极端月份一眼可见。</div></div>
</div>

<h2>近端滚动监测</h2>
<div class="desc">L1 滚动窗口 ICIR20（w20 = 20 日窗 / w60 = 60 日窗）——信号衰减的第一现场</div>
<div class="card"><div id="chart_roll"></div><div class="note">滚动 ICIR 跌破 ±0.05 即提示衰减；近 1 年 ICIR20 = __Y1_ICIR20__（__Y1_DAYS__ 个成熟日）。</div></div>

<h2>风格暴露</h2>
<div class="desc">与 6 个风格代表因子的逐日截面 spearman（因子湖现算，全史 + 年度演变）</div>
<div class="grid g2">
  <div class="card"><b>风格暴露（全期截面 spearman 均值 ρ）</b>
    <div id="chart_style"></div><div class="note">__STYLE_NOTE__</div></div>
  <div class="card"><b>风格暴露年度演变</b>
    <table id="tbl_style" style="margin-top:6px"></table>
    <div class="note">暴露逐年变动大 = 风格身份随制度切换，依赖该暴露的收益须做风格正交化检验。</div></div>
</div>

<h2>分组分析（h=20 前瞻）</h2>
<div class="desc">Q1 = 因子值最低组、Q5 = 最高组；收益为 20 日远期逐日摊薄（非可交易组合，终审须组合层 A/B）。十分位 = mirror 日线 adj close LEAD 20，先剔无前瞻行再 NTILE(10)，与 v3 同口径。</div>
<div class="card"><b>分组收益（五分位 + 多空，h=20 摊薄）</b>
  <table id="tbl_groups" style="margin-top:8px"></table>
  <div class="note">__GRP_NOTE__</div>
</div>
<div class="card" style="margin-top:12px"><b>十分位分析（D1 = 低值 10% → D10 = 高值 10%，bp/20日，全期池化均值）</b>
  <table id="tbl_decile" style="margin-top:8px"></table>
  <div class="note" id="decile_note"></div>
</div>
<div class="grid g2" style="margin-top:12px">
  <div class="card"><b>分组净值曲线（h=20 摊薄，金线 = 多空 Q5−Q1）</b>
    <div id="chart_nav"></div></div>
  <div class="card"><b>年度分解（IC20 + 分组收益，bp/20日）</b>
    <table id="tbl_yearly" style="margin-top:6px"></table>
    <div class="note">__YEARLY_NOTE__</div></div>
</div>

<h2>信息增量</h2>
<div class="desc">与核心生产因子的逐日截面相关（factor_corr_daily 湖）——重叠度高 = 边际贡献小。正交化残差 IC = 因子原始值对核心生产因子逐日截面 OLS 取残差后的 rank IC（h=20 前瞻，湖内自算与 v3 同口径），衡量对现有组合的边际贡献。</div>
<div class="card"><b>与核心生产因子截面相关（近 250 日均值 spearman）</b>
  <table style="margin-top:8px">
    <tr><th>核心因子</th><th>ρ（近 250 日）</th><th>天数</th><th>判定</th></tr>
__CORR_ROWS__
  </table>
  <div class="note">__CORR_NOTE__</div>
</div>
<div class="card" style="margin-top:12px"><b>正交化残差 IC（对 size/amihud_20 的边际贡献）</b>
  <div class="grid g4" id="resid_block" style="margin-top:8px"></div>
  <div class="note" id="resid_note"></div>
</div>

<h2>拥挤度（库内代理）</h2>
<div class="desc">crowding 湖最新快照 + 镜像信号。完整拥挤度评估需公募持仓 / 两融等外部数据。</div>
<div class="grid g4">
  <div class="metric"><div class="k">综合拥挤度</div><div class="v">__CROWD_C__</div><div class="s">__CROWD_DATE__</div></div>
  <div class="metric"><div class="k">历史分位</div><div class="v">__CROWD_PCT__</div><div class="s">自身历史时序分位</div></div>
  <div class="metric"><div class="k">相关 z / 估值 z</div><div class="v">__CROWD_CORRZ__ / __CROWD_VALZ__</div><div class="s">同向相关拥挤 / 价格偏离</div></div>
  <div class="metric"><div class="k">换手分位</div><div class="v">__CROWD_TO__</div><div class="s">交易热度</div></div>
</div>

<h2>最新截面 Top 10（因子值最高）</h2>
<div class="card">
  <table id="tbl_latest"></table>
  <div class="note">PIT 口径：因子值基于当日及以前公开数据计算，无 as-of 回填、无前视。方向解释见分组分析（Q5 = 高值组）。</div>
</div>

<footer>
  口径附注：本看板为<b>湖模式 v1.1</b>（零主库依赖，主库写锁期间可批量生成）——IC/分组 = metric_store L0（信号日口径，h=20 成熟边界 T-20 起非空）；IC 谱为 h=1/5/20/60/120 五点（audit 快照）；半衰期 = 五点谱指数拟合；<b>十分位与正交化残差 IC = L0 扩展湖 factor_metric_ext 预计算列（重截面计算一次性落库，页面仅轻聚合；口径与 v3 同源：mirror adj close LEAD 20，残差对 size/amihud_20 原始值 OLS，|raw_ic|&gt;0.03 且 |resid_ic|&lt;0.5|raw_ic| 判增量微弱）</b>；风格暴露 = 因子湖 6 风格代表因子截面 spearman；信息增量 = factor_corr_daily（9 核心因子）；拥挤度 = crowding 湖快照（库内代理）；FDR q / PIT = 审计快照。红绿灯仅初筛，入生产候选须组合层 A/B 终审。<br>
  数据源：metric_store L0/L1 + factor_metric_ext + factor_corr_daily + crowding 湖 + 因子湖 + 审计/FDR/registry（全只读）｜生成：__GENERATED__｜模板 dashboard-lake-v1.1（scripts/factor_eval/build_all_center_lake.py，耗时 __ELAPSED__s）
</footer>
</div>

<script>
const DATA = __DATA__;
const C={q1:'#5da9e0',q2:'#7bb8e8',q3:'#9a9a9a',q4:'#e0a35d',q5:'#e05d5d',ls:'#e6b455',ic:'#e05d5d',icir:'#55c4c4'};
function svgEl(w,h){const s=document.createElementNS('http://www.w3.org/2000/svg','svg');
  s.setAttribute('viewBox','0 0 '+w+' '+h);s.setAttribute('width','100%');return s}
function add(s,tag,attrs,txt){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);
  for(const k in attrs)e.setAttribute(k,attrs[k]);if(txt!=null)e.textContent=txt;s.appendChild(e);return e}
function lineChart(el,xs,series,xlabels,fmt){
  const W=520,H=230,L=46,R=52,T=16,B=30,w=W-L-R,h=H-T-B;
  const s=svgEl(W,H);el.appendChild(s);
  const main=series.filter(p=>!p.axis2), sec=series.filter(p=>p.axis2);
  const ymin=Math.min(...main.flatMap(p=>p.data)),ymax=Math.max(...main.flatMap(p=>p.data));
  let ymin2=0,ymax2=1;
  if(sec.length){ymin2=Math.min(...sec.flatMap(p=>p.data));ymax2=Math.max(...sec.flatMap(p=>p.data));
    const pad2=(ymax2-ymin2)*0.08||0.05;ymin2-=pad2;ymax2+=pad2;}
  const X=i=>L+(xs.length>1?i/(xs.length-1)*w:0);
  const Y=v=>T+h-(v-ymin)/(ymax-ymin)*h, Y2=v=>T+h-(v-ymin2)/(ymax2-ymin2)*h;
  for(let g=0;g<=4;g++){
    const v=ymin+(ymax-ymin)*g/4;
    add(s,'line',{x1:L,y1:Y(v),x2:W-R,y2:Y(v),stroke:'#333','stroke-width':1});
    add(s,'text',{x:L-5,y:Y(v)+3,'text-anchor':'end',fill:'#9a9a9a','font-size':9},fmt?fmt(v):v.toFixed(2));
    if(sec.length){const v2=ymin2+(ymax2-ymin2)*g/4;
      add(s,'text',{x:W-R+6,y:Y2(v2)+3,fill:'#55c4c4','font-size':9},v2.toFixed(2));}}
  if(sec.length){add(s,'line',{x1:W-R,y1:T,x2:W-R,y2:T+h,stroke:'#55c4c4','stroke-width':1,opacity:0.5});}
  const nx=Math.min(xs.length,8);
  for(let i=0;i<nx;i++){const xi=Math.round(i*(xs.length-1)/(nx-1));
    add(s,'text',{x:X(xi),y:H-10,'text-anchor':'middle',fill:'#9a9a9a','font-size':9},xlabels?xlabels[xi]:xs[xi]);}
  series.forEach(p=>{
    const pts=p.data.map((v,i)=>[X(i),p.axis2?Y2(v):Y(v)]).filter(pt=>pt[1]===pt[1]&&isFinite(pt[1]));
    if(pts.length>1)add(s,'polyline',{fill:'none',stroke:p.color,'stroke-width':2,
      points:pts.map(pt=>pt[0]+','+pt[1]).join(' ')});});
  let lx=L+4;series.forEach(p=>{add(s,'circle',{cx:lx,cy:T+2,r:3,fill:p.color});
    const t=add(s,'text',{x:lx+7,y:T+5,fill:'#bbb','font-size':10},p.name);lx+=7+t.getComputedTextLength()+16;});
}
/* 多周期谱 */
(function(){
  const hs=DATA.spec.h,el=document.getElementById('chart_decay');
  lineChart(el,hs,[{name:'IC',color:C.ic,data:DATA.spec.ic},
    {name:'ICIR（右轴）',color:C.icir,data:DATA.spec.icir,axis2:true}],
    hs.map(String),v=>v.toFixed(2));
})();
/* IC5 直方图 */
(function(){const el=document.getElementById('chart_hist');
  const W=520,H=230,L=46,R=12,T=14,B=30,w=W-L-R,h=H-T-B;
  const s=svgEl(W,H);el.appendChild(s);
  const arr=DATA.ic5_series,nb=30,mn=Math.min(...arr),mx=Math.max(...arr);
  if(mx===mn){el.textContent='数据不足';return;}
  const bins=new Array(nb).fill(0);
  arr.forEach(v=>{let i=Math.floor((v-mn)/(mx-mn)*nb);if(i>=nb)i=nb-1;bins[i]++;});
  const bm=Math.max(...bins);
  bins.forEach((b,i)=>{const bw=w/nb;
    add(s,'rect',{x:L+i*bw+0.5,y:T+h-b/bm*h,width:bw-1,height:b/bm*h,fill:'#e05d5d',opacity:0.85});});
  const mean=DATA.s5.mean,X=v=>L+(v-mn)/(mx-mn)*w;
  add(s,'line',{x1:X(mean),y1:T,x2:X(mean),y2:T+h,stroke:'#e6b455','stroke-width':1.5,'stroke-dasharray':'4 3'});
  add(s,'text',{x:X(mean)+4,y:T+10,fill:'#e6b455','font-size':10},'mean '+mean);
  for(let g=0;g<=4;g++){const v=mn+(mx-mn)*g/4;
    add(s,'text',{x:X(v),y:T+h+14,'text-anchor':'middle',fill:'#9a9a9a','font-size':9},v.toFixed(2));}})();
/* 自相关 */
(function(){const el=document.getElementById('chart_acf');
  const W=520,H=230,L=46,R=12,T=14,B=30,w=W-L-R,h=H-T-B;
  const s=svgEl(W,H);el.appendChild(s);
  const acf=DATA.ic5_acf;
  add(s,'line',{x1:L,y1:T+h/2,x2:W-R,y2:T+h/2,stroke:'#555'});
  acf.forEach((v,i)=>{const bw=w/acf.length,x=L+i*bw;
    const hh=Math.abs(v)*h/2;
    add(s,'rect',{x:x+bw*0.25,y:v>=0?T+h/2-hh:T+h/2,width:bw*0.5,height:Math.max(hh,0.5),
      fill:v>=0?'#55c4c4':'#e05d5d'});
    add(s,'text',{x:x+bw*0.5,y:H-10,'text-anchor':'middle',fill:'#9a9a9a','font-size':9},(i+1));});})();
/* 月度热力 */
(function(){const t=document.getElementById('tbl_monthly');
  const m=DATA.monthly;
  if(!m.years.length){t.innerHTML='<tr><td>无数据</td></tr>';return;}
  let html='<tr><th>年</th>';m.months.forEach(x=>html+='<th>'+x+'月</th>');html+='</tr>';
  m.years.forEach((y,i)=>{html+='<tr><td>'+y+'</td>';
    m.values[i].forEach(v=>{let bg='',c='#777';
      if(v!=null){bg='rgba(224,93,93,'+Math.min(Math.abs(v)*1.8,0.85)+')';c=v>0.15?'#fff':'#bbb';
        if(v<0)bg='rgba(93,201,142,'+Math.min(Math.abs(v)*1.8,0.85)+')';}
      html+='<td style="background:'+bg+';color:'+c+'">'+(v==null?'—':(v>0?'+':'')+v.toFixed(3))+'</td>';});
    html+='</tr>';});
  t.innerHTML=html;})();
/* 滚动 ICIR */
(function(){const el=document.getElementById('chart_roll');
  if(!DATA.roll){el.textContent='L1 无该因子滚动数据';return;}
  lineChart(el,DATA.roll.dates,[{name:'ICIR20 w20',color:C.ic,data:DATA.roll.w20},
    {name:'ICIR20 w60',color:C.icir,data:DATA.roll.w60}],DATA.roll.dates,v=>v.toFixed(2));})();
/* 风格暴露 */
(function(){const el=document.getElementById('chart_style');
  const ent=Object.entries(DATA.style_exp);
  if(!ent.length){el.textContent='风格矩阵数据不足';return;}
  const W=520,H=250,L=110,R=52,T=14,B=10;
  const gap=16,bh=(H-T-B-gap*(ent.length-1))/ent.length;
  const s=svgEl(W,H);el.appendChild(s);
  const mx=Math.max(...ent.map(e=>Math.abs(e[1].all)))*1.18||0.1;
  const w=W-L-R,X0=L+w/2;
  add(s,'line',{x1:X0,y1:T,x2:X0,y2:H-B,stroke:'#555'});
  ent.forEach((e,i)=>{const y=T+i*(bh+gap),rho=e[1].all;
    const bw=Math.abs(rho)/mx*(w/2),x=rho>=0?X0:X0-bw;
    const col=rho>=0?'#e05d5d':'#5dc98e';
    add(s,'rect',{x,y:y+2,width:Math.max(bw,1),height:bh-4,fill:col,opacity:0.85});
    add(s,'text',{x:X0-8,y:y+bh/2+3,'text-anchor':'end',fill:'#ccc','font-size':11},e[0]);
    add(s,'text',{x:(rho>=0?X0+bw+6:X0-bw-6),y:y+bh/2+3,'text-anchor':rho>=0?'start':'end',fill:col,'font-size':11,'font-weight':700},(rho>=0?'+':'')+rho.toFixed(3));});})();
(function(){const t=document.getElementById('tbl_style');
  const ent=Object.entries(DATA.style_exp);
  if(!ent.length){t.innerHTML='<tr><td>无数据</td></tr>';return;}
  const years=[...new Set(ent.flatMap(e=>Object.keys(e[1].yearly).map(String)))].sort();
  let html='<tr><th>风格</th>';years.forEach(y=>html+='<th>'+y+'</th>');html+='<th>全期</th></tr>';
  ent.forEach(([k,v])=>{html+='<tr><td>'+k+'</td>';
    years.forEach(y=>{const r=v.yearly[y];
      html+=r==null?'<td>—</td>':'<td class="'+(r>=0?'pos':'neg')+'">'+(r>=0?'+':'')+r.toFixed(3)+'</td>';});
    html+='<td class="'+(v.all>=0?'pos':'neg')+'" style="font-weight:700">'+(v.all>=0?'+':'')+v.all.toFixed(3)+'</td></tr>';});
  t.innerHTML=html;})();
/* 分组表 + 净值 */
(function(){const t=document.getElementById('tbl_groups');
  const order=['Q1','Q2','Q3','Q4','Q5','LS'];
  let html='<tr><th>分组</th><th>均值(bp/20日)</th><th>年化收益</th><th>年化波动</th><th>夏普</th><th>最大回撤</th><th>月度胜率</th></tr>';
  order.forEach(k=>{const g=DATA.groups[k];if(!g){return;}
    html+='<tr><td>'+(k==='LS'?'多空（Q5−Q1）':k)+'</td>'+
      '<td>'+(g.bp>0?'+':'')+g.bp+'</td>'+
      '<td class="'+(g.ann>=0?'pos':'neg')+'">'+(g.ann*100).toFixed(1)+'%</td>'+
      '<td>'+(g.vol*100).toFixed(1)+'%</td><td>'+(g.sharpe==null?'—':g.sharpe)+'</td>'+
      '<td class="neg">'+(g.mdd*100).toFixed(1)+'%</td><td>'+(g.monwin*100).toFixed(0)+'%</td></tr>';});
  t.innerHTML=html;})();
(function(){const el=document.getElementById('chart_nav');
  const order=['Q1','Q2','Q3','Q4','Q5'];
  const series=order.filter(k=>DATA.navs[k]&&DATA.navs[k].length>1)
    .map(k=>({name:k,color:C[k.toLowerCase()],data:DATA.navs[k]}));
  if(DATA.navs.LS&&DATA.navs.LS.length>1)series.push({name:'多空',color:C.ls,data:DATA.navs.LS});
  if(!series.length){el.textContent='无数据';return;}
  lineChart(el,series[0].data.map((_,i)=>i),series,DATA.nav_dates,v=>v.toFixed(2));})();
(function(){const t=document.getElementById('tbl_yearly');
  let html='<tr><th>年</th><th>天数</th><th>IC20</th><th>ICIR</th><th>Q1</th><th>Q5</th><th>多空(bp/20日)</th></tr>';
  DATA.yearly.forEach(r=>{html+='<tr><td>'+r.year+'</td><td>'+r.n+'</td>'+
    (r.ic==null?'<td>—</td>':'<td class="'+(r.ic>0?'pos':'neg')+'">'+r.ic.toFixed(4)+'</td>')+
    '<td>'+(r.icir==null?'—':r.icir)+'</td>'+
    '<td class="'+(r.q1>0?'pos':'neg')+'">'+(r.q1*1e4).toFixed(0)+'</td>'+
    '<td class="'+(r.q5>0?'pos':'neg')+'">'+(r.q5*1e4).toFixed(0)+'</td>'+
    '<td class="'+(r.ls>0?'pos':'neg')+'">'+(r.ls*1e4).toFixed(0)+'</td></tr>';});
  t.innerHTML=html;})();
/* 最新 top10 */
(function(){const t=document.getElementById('tbl_latest');
  let html='<tr><th>日期</th><th>股票代码</th><th>因子值</th></tr>';
  DATA.latest.top.forEach(r=>{html+='<tr><td>'+DATA.latest.date+'</td><td>'+r.code+'</td><td>'+r.value+'</td></tr>';});
  t.innerHTML=html||'<tr><td>无数据</td></tr>';})();
/* 十分位 */
(function(){const t=document.getElementById('tbl_decile');
  const dn=document.getElementById('decile_note');
  if(!DATA.decile||!DATA.decile.bp){t.innerHTML='<tr><td>无数据</td></tr>';
    if(dn)dn.textContent='因子或前瞻收益覆盖不足，十分位未计算。';return;}
  const bp=DATA.decile.bp;
  let html='<tr>';
  bp.forEach((v,i)=>{html+='<th>D'+(i+1)+(i===0?'（低值）':i===9?'（高值）':'')+'</th>';});
  html+='</tr><tr>';
  bp.forEach(v=>{html+='<td class="'+(v>0?'pos':'neg')+'">'+(v>0?'+':'')+v+'</td>';});
  html+='</tr>';
  t.innerHTML=html;
  if(dn){const mono=DATA.decile.mono;
    dn.textContent='单调性 spearman（D1→D10 收益 vs 序）= '+(mono==null?'—':mono)+
      '｜D10−D1 价差 = '+DATA.decile.spread_bp+' bp/20日｜有效交易日 '+DATA.decile.n_days+
      '。单调性好 = 因子排序信息在尾部和头部一致；仅尾部显著 = 头部噪声大。';}})();
/* 正交化残差 IC */
(function(){const el=document.getElementById('resid_block');
  const nt=document.getElementById('resid_note');
  if(!DATA.resid){el.innerHTML='<div class="metric"><div class="v">无数据</div><div class="s">因子覆盖不足或自身为正交核心</div></div>';
    if(nt)nt.textContent='—';return;}
  const r=DATA.resid;
  el.innerHTML=
    '<div class="metric"><div class="k">原始 IC（h=20）</div><div class="v">'+(r.raw_ic>0?'+':'')+r.raw_ic+'</div><div class="s">正交化前</div></div>'+
    '<div class="metric"><div class="k">残差 IC</div><div class="v '+(Math.abs(r.resid_ic)<0.02?'':'')+'">'+(r.resid_ic>0?'+':'')+r.resid_ic+'</div><div class="s">剔除核心因子后</div></div>'+
    '<div class="metric"><div class="k">原始 / 残差 ICIR</div><div class="v">'+(r.raw_icir==null?'—':r.raw_icir)+' / '+(r.resid_icir==null?'—':r.resid_icir)+'</div><div class="s">稳定性对比</div></div>'+
    '<div class="metric"><div class="k">有效天数</div><div class="v">'+r.n_days+'</div><div class="s">正交核心：'+r.cores.join(' + ')+'</div></div>';
  if(nt){const weak=Math.abs(r.raw_ic)>0.03&&Math.abs(r.resid_ic)<0.5*Math.abs(r.raw_ic);
    nt.textContent=weak?'⚠️ 正交化后增量微弱：残差 IC '+r.resid_ic.toFixed(4)+' 不足原始 '+r.raw_ic.toFixed(4)+' 的一半——因子信息大部分被核心生产因子解释，对现有组合的边际贡献有限。'
      :'残差 IC 与原始 IC 量级相当——剔除核心因子后仍保留独立预测信息，对现有组合有边际贡献。';}})();
</script>
</body>
</html>"""
    I, I20 = D["s5"], D["s20"]
    I1y = D["s20_1y"]
    pit_cls = ("var(--green)" if X["verdict"] == "PASS"
               else ("var(--red)" if X["verdict"] == "FAIL" else "var(--gold)"))
    head = ("红色：" if D["light"] == "🔴" else "黄色：" if D["light"] == "🟡"
            else "绿色：") + X["verdict_summary"]
    spec_h = list(D["spec"].keys()) if D["spec"] else []
    R = {
        "__NAME__": X["name"], "__DESC__": X["desc"],
        "__CATEGORY__": X["category"], "__RATIONALE__": D["rationale"],
        "__START__": X["start"], "__END__": X["end"],
        "__L0_END__": X["l0_end"], "__GENERATED__": X["generated"],
        "__ELAPSED__": str(X["elapsed"]),
        "__NPOOL__": str(X["n_pool"]), "__NAUDITED__": str(X["n_audited"]),
        "__DIRN__": ("+" if D["dirn"] > 0 else "−") + str(abs(D["dirn"])),
        "__IC5__": f"{I.get('mean', 0):+.4f}" if I else "—",
        "__IC5_CLS__": "pos" if I and I.get("mean", 0) > 0 else "neg",
        "__ICIR5__": f"{I['icir']:+.3f}" if I else "—",
        "__IC20__": f"{I20['mean']:+.4f}" if I20 else "—",
        "__ICIR20__": f"{I20['icir']:+.3f}" if I20 else "—",
        "__WIN5__": f"{I['win']*100:.0f}%" if I else "—",
        "__IC5_N__": str(I["n"]) if I else "0",
        "__SKEW__": f"{I['skew']:.3f}" if I else "—",
        "__KURT__": f"{I['kurt']:.3f}" if I else "—",
        "__Y1_ICIR20__": (f"{I1y['icir']:+.3f}" if I1y else "—"),
        "__Y1_DAYS__": str(I1y["n"]) if I1y else "0",
        "__FDR_Q__": str(D["q"]) if D["q"] is not None else "—",
        "__T_LIB__": (str(D["t_lib"]) if D["t_lib"] is not None else "—"),
        "__PIT__": X["verdict"], "__PIT_CLS__": pit_cls,
        "__HALF__": D["half"], "__HALF_NOTE__": D["half_note"],
        "__LIGHT__": D["light"],
        "__VERDICT_HEAD__": head,
        "__VERDICT_SUMMARY__": X["verdict_summary"] or "无风险标记",
        "__VERDICT_TABLE__": X["verdict_table"],
        "__DECAY_NOTE__": X["decay_note"],
        "__HIST_NOTE__": X["hist_note"], "__ACF_NOTE__": X["acf_note"],
        "__STYLE_NOTE__": X["style_note"], "__GRP_NOTE__": X["grp_note"],
        "__YEARLY_NOTE__": X["yearly_note"], "__CORR_ROWS__": X["corr_rows"],
        "__CORR_NOTE__": X["corr_note"],
        "__CROWD_C__": X["crowd_c"], "__CROWD_PCT__": X["crowd_pct"],
        "__CROWD_CORRZ__": X["crowd_corrz"], "__CROWD_VALZ__": X["crowd_valz"],
        "__CROWD_TO__": X["crowd_to"], "__CROWD_DATE__": X["crowd_date"],
        "__DATA__": payload,
    }
    html = TEMPLATE
    for k, v in R.items():
        html = html.replace(k, str(v))
    return html


# ───────────────────────────── 批量主流程 ─────────────────────────────
def _fmt_verdict_table(D: dict, corr_rows: list) -> str:
    def cell(o):
        return {"✅": "ok", "🟡": "warn", "❌": "warn"}[o]
    s5, s20 = D["s5"], D["s20"]
    rows = [
        ("PIT / 结构审计", D["verdict"],
         "✅" if D["verdict"] == "PASS" else ("❌" if D["verdict"] == "FAIL" else "🟡")),
        ("FDR q（BH + 自相关折减，库级 h=20）",
         str(D["q"]) if D["q"] is not None else "无记录",
         ("✅" if D["q"] < 0.05 else ("🟡" if D["q"] < 0.25 else "❌"))
         if D["q"] is not None else "❌"),
        ("现场重算 IC5 / ICIR5（L0 全史）",
         f"{s5['mean']:+.4f} / {s5['icir']:+.3f}（{s5['n']} 日）" if s5 else "—",
         "✅" if s5 and abs(s5["icir"]) > 0.1 else "🟡"),
        ("近 1 年 ICIR20", f"{D['s20_1y']['icir']:+.3f}" if D["s20_1y"] else "—",
         "✅" if D["s20_1y"] and abs(D["s20_1y"]["icir"]) > 0.1 else "🟡"),
        ("年度 IC20 反号", f"{D['n_neg']}/{len(D['yearly'])} 年",
         "✅" if D["n_neg"] < 2 else "🟡"),
        ("经济机制登记", "已登记" if D["rationale"] != "未登记" else "未登记",
         "✅" if D["rationale"] != "未登记" else "❌"),
        ("核心因子冗余（|ρ|>0.7）",
         " ｜ ".join(f"{c['core']} {c['rho']:+.2f}" for c in corr_rows
                     if c["redundant"]) or "无",
         "🟡" if any(c["redundant"] for c in corr_rows) else "✅"),
        ("近端滚动 ICIR20（w60 最新）",
         str(D["roll"]["w60"][-1]) if D["roll"] and D["roll"]["w60"]
         and D["roll"]["w60"][-1] is not None else "—",
         "✅" if D["roll"] and D["roll"]["w60"] and D["roll"]["w60"][-1] is not None
         and abs(D["roll"]["w60"][-1]) > 0.1 else "🟡"),
    ]
    return ("<table style=\"margin-top:12px\"><tr><th>检验项</th><th>结果</th>"
            "<th>判定</th></tr>" + "".join(
                f"<tr><td>{t}</td><td>{r}</td>"
                f"<td class=\"{cell(o)}\">{o}</td></tr>" for t, r, o in rows)
            + "</table>")


def _texts(D: dict) -> dict:
    """数据驱动的解读文案"""
    se = D["style_exp"]
    if se:
        order = sorted(se, key=lambda k: -abs(se[k]["all"]))
        dom = order[0]
        vals = list(se[dom]["yearly"].values())
        drift = max(vals) - min(vals) if len(vals) > 1 else 0
        style_note = (f"暴露最强的是 {dom}（ρ={se[dom]['all']:+.3f}，"
                      f"{se[dom]['n_days']} 日）"
                      + (f"，年度变动 {drift:.3f}" if len(vals) > 1 else "")
                      + ("——近市值风格的结构化表达，风格中性化检验为入库必做项。"
                         if dom == "size" and abs(se[dom]["all"]) > 0.5
                         else "。"))
    else:
        style_note = "风格矩阵数据不足，未计算。"
    grp = D["groups"]
    best = max((k for k in ("Q1", "Q5") if grp.get(k)),
               key=lambda k: grp[k]["bp"] * (1 if D["dirn"] > 0 else -1),
               default=None)
    grp_note = ((f"方向 = {D['dirn']:+d}："
                 f"{'高值组 Q5' if D['dirn'] > 0 else '低值组 Q1'} 为多头端"
                 f"（{best} 年化 {grp[best]['ann']*100:.1f}%，夏普 "
                 f"{grp[best]['sharpe']}）" if best and grp.get(best) else "")
                + "。多空腿含做空约束：A 股低成本做空不可得，预测力集中在空头腿时不可收割。")
    bad = [r["year"] for r in D["yearly"]
           if r["ic"] is not None and r["ic"] * D["dirn"] < 0]
    yearly_note = (f"{', '.join(map(str, bad))} 年 IC20 与信号方向相反——"
                   "年度级衰减证据。" if bad else "各年度 IC20 均与方向一致。")
    corr_red = [c for c in D["corr_rows"] if c["redundant"]]
    corr_note = ("|ρ|>0.7 = 信息高度重叠，入库价值须按边际贡献重排。"
                 if corr_red else "与全部核心因子 |ρ|≤0.7——独立信息源，组合分散价值大。")
    s5 = D["s5"]
    hist_note = ("左偏 = 存在深负坏月份（见月度热力图）。"
                 if s5 and s5["skew"] < -0.3 else
                 "右偏 = 好月份拉动均值，常态弱于表观。" if s5 and s5["skew"] > 0.3
                 else "近似对称分布。")
    acf_note = (f"lag1 ρ={s5['rho']:.3f}：h=5 相邻窗口共享 4/5 收益，"
                "多重检验 N_eff 折减以此为据。" if s5 else "")
    spk = D["spec"]
    if spk:
        peak_h = max(spk, key=lambda h: abs(spk[h]["rank"]["mean"]))
        decay_note = (f"峰值 |IC| {abs(spk[peak_h]['rank']['mean']):.4f} 在 "
                      f"h={peak_h}，半衰期 {D['half']} 日。"
                      + D["half_note"])
    else:
        decay_note = "谱数据不足。"
    return {"style_note": style_note, "grp_note": grp_note,
            "yearly_note": yearly_note, "corr_note": corr_note,
            "hist_note": hist_note, "acf_note": acf_note,
            "decay_note": decay_note}


def run_one(name: str, P: dict, n_pool: int, n_audited: int) -> Path:
    t0 = time.time()
    D = compute_one(name, P)
    texts = _texts(D)
    reg_desc = ""
    try:
        from quantlab.factor.registry import all_factors
        fobj = all_factors().get(name)
        if fobj is not None:
            reg_desc = getattr(fobj, "description", "") or ""
    except Exception:
        pass
    crowd = D["crowd"] or {}
    X = {
        "name": name,
        "desc": reg_desc or name,
        "category": (P["audits"].get(name, {}) or {}).get("category", ""),
        "start": f"{P['l0']['date'].min():%Y-%m-%d}",
        "end": f"{P['l0_end']:%Y-%m-%d}",
        "l0_end": f"{P['l0_end']:%Y-%m-%d}",
        "generated": f"{datetime.now():%Y-%m-%d %H:%M}",
        "elapsed": round(time.time() - t0, 1),
        "n_pool": n_pool, "n_audited": n_audited,
        "verdict": D["verdict"],
        "verdict_summary": "；".join(D["flags"]) if D["flags"] else "无风险标记",
        "verdict_table": _fmt_verdict_table(D, D["corr_rows"]),
        "decay_note": texts["decay_note"], "hist_note": texts["hist_note"],
        "acf_note": texts["acf_note"], "style_note": texts["style_note"],
        "grp_note": texts["grp_note"], "yearly_note": texts["yearly_note"],
        "corr_note": texts["corr_note"],
        "corr_rows": "\n    ".join(
            f"<tr><td>{c['core']}</td>"
            f"<td class=\"{'pos' if c['rho'] > 0 else 'neg'}\">{c['rho']:+.3f}</td>"
            f"<td>{c['n']}</td>"
            + ("<td class=\"warn\">⚠️ 冗余（|ρ|&gt;0.7）</td>" if c["redundant"]
               else "<td class=\"ok\">独立</td>") + "</tr>"
            for c in D["corr_rows"]) or "<tr><td colspan=\"4\">无数据</td></tr>",
        "crowd_c": f"{crowd['crowding']:+.2f}" if crowd and "crowding" in crowd else "—",
        "crowd_pct": f"{crowd['hist_pct']*100:.0f}%" if crowd and "hist_pct" in crowd else "—",
        "crowd_corrz": f"{crowd['corr_z']:+.2f}" if crowd and "corr_z" in crowd else "—",
        "crowd_valz": f"{crowd['val_z']:+.2f}" if crowd and "val_z" in crowd else "—",
        "crowd_to": f"{crowd['turnover_pct']*100:.0f}%" if crowd and "turnover_pct" in crowd else "—",
        "crowd_date": crowd.get("date", "") if crowd else "",
    }
    html = build_html(D, X)
    DOCS.mkdir(exist_ok=True)
    out = DOCS / f"factor_center_{name}.html"
    out.write_text(html, encoding="utf-8")

    idx = (json.loads(INDEX.read_text(encoding="utf-8"))
           if INDEX.exists() else [])
    ts = datetime.now()
    idx.append({
        "id": f"dashboard:{name}:{ts:%Y%m%d%H%M}",
        "type": "dashboard", "name": name,
        "date": f"{ts:%Y-%m-%d}", "generated_at": ts.isoformat(timespec="seconds"),
        "window": f"L0 全史 {X['start']}~{X['end']}（湖模式）",
        "tags": ["lake-mode"], "light": D["light"], "flags": D["flags"],
        "file": str(out.relative_to(ROOT)).replace("\\", "/"),
        "template": "dashboard-lake-v1.1",
        "summary": (f"IC5 {D['s5']['mean']:+.4f} 灯 {D['light']}"
                    f"｜1Y ICIR20 {(D['s20_1y'] or {}).get('icir', '—')}"),
    })
    INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="全因子单因子看板批量生成（湖模式）")
    p.add_argument("--pool", default="ashare_ex")
    p.add_argument("--only", default="", help="逗号分隔因子名（调试用）")
    p.add_argument("--retry", action="store_true", help="只重跑上次失败清单")
    a = p.parse_args()

    P = preload(a.pool)
    names = P["names"]
    if a.only:
        names = [n.strip() for n in a.only.split(",") if n.strip()]
    elif a.retry and FAIL_LOG.exists():
        names = json.loads(FAIL_LOG.read_text(encoding="utf-8")).get("failed", [])
    n_pool = len([d for d in FACTOR_DIR.iterdir() if d.is_dir()])
    n_audited = len(list(AUDIT_DIR.glob("*.json")))

    ok, failed = [], []
    t0 = time.time()
    for i, name in enumerate(names, 1):
        try:
            out = run_one(name, P, n_pool, n_audited)
            ok.append(name)
            if i % 10 == 0 or i == len(names):
                print(f"  [{i}/{len(names)}] {out.name} "
                      f"({time.time()-t0:.0f}s elapsed)")
        except Exception as e:
            failed.append(name)
            print(f"  [FAIL] {name}: {e}", file=sys.stderr)
            traceback.print_exc()
    FAIL_LOG.write_text(json.dumps({"failed": failed, "ts": f"{datetime.now():%Y-%m-%d %H:%M}"},
                                   ensure_ascii=False), encoding="utf-8")
    print(f"[done] 成功 {len(ok)} / 失败 {len(failed)}"
          f"（{time.time()-t0:.0f}s）")
    if failed:
        print(f"  失败清单：{failed[:20]}{'…' if len(failed) > 20 else ''}")
        print(f"  重跑：python {Path(__file__).name} --retry")


if __name__ == "__main__":
    main()
