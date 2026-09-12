"""因子看板标准模板构建器（factor-model-evaluator skill 的 HTML 产出）
======================================================================
将 v3 看板（docs/factor_center_amihud_20.html）固化为通用模板：任意因子
一键生成同款看板，所有区块与文案数据驱动、无因子特定硬编码。

用法：
  python scripts/factor_eval/build_dashboard.py --name amihud_20 \
      --start 2023-01-01 --end 2026-09-11 [--tags production]

产出：
  docs/factor_center_{name}.html（单文件无外部依赖，浏览器直接打开）
  reports/factor_eval/index.json 登记 type=dashboard 条目（可检索）

模板结构（七区）：概览指标 → 结论摘要（红绿灯+判定逐项表）→ IC 分析
（多周期谱/分布/自相关/月度热力）→ 风格分解 → 分组分析（五分位+净值+
十分位+多空腿+年度）→ 信息增量（正交化+核心因子相关）→ 拥挤度 → 最新数据。

口径：IC 主口径 = 5 日 rank IC（--h-ic 可改）；分组收益 = 20 日远期
逐日摊薄（调仓周期口径，--h-grp 可改）。

数据源全只读：因子湖 parquet + DuckDB READ_ONLY ATTACH（用后 DETACH）+
factor_audit JSON + FDR JSON + registry。计算复用 run_eval.py。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from run_eval import (  # noqa: E402
    FACTOR_DIR, DB, audit_info, fdr_info, registry_info,
    factor_daily_metrics, factor_horizon_structure, factor_decile,
    factor_residual_ic, factor_core_corr,
)

DOCS = ROOT / "docs"
INDEX = ROOT / "reports" / "factor_eval" / "index.json"
STYLES = {"size": "市值", "bp": "账面市值比（价值）", "momentum_20": "动量（20日）",
          "volatility_20": "波动率", "turnover": "换手率", "reversal_5": "短期反转"}


# ─────────────────── 模板专属计算（demo 数据脚本移植+参数化） ───────────────────
def pearson_ic(name: str, h: int, start: str, end: str) -> float:
    parts = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    plist = [str(p) for p in parts]
    con = duckdb.connect()
    con.execute(f"ATTACH '{DB}' AS maindb (READ_ONLY)")
    q = f"""
    WITH f AS (SELECT date, code, value FROM read_parquet({plist})
               WHERE date BETWEEN DATE '{start}' AND DATE '{end}'),
    px AS (SELECT date, code, close * adj_factor AS c FROM maindb.kline_daily),
    base AS (
        SELECT f.date, f.code, f.value AS v,
               LEAD(px.c, {h}) OVER (PARTITION BY f.code ORDER BY f.date) / px.c - 1 AS fwd
        FROM f JOIN px ON f.date = px.date AND f.code = px.code)
    SELECT date, corr(v, fwd) AS ic FROM base
    WHERE fwd IS NOT NULL GROUP BY date ORDER BY date
    """
    s = con.execute(q).df()
    con.execute("DETACH maindb")
    con.close()
    return float(s["ic"].dropna().mean())


def ic_type_matrix(name: str, start: str, end: str,
                   horizons: tuple = (1, 5, 20)) -> dict:
    """Pearson IC 与 Rank IC 双类型 × 多持有期矩阵（单连接一次算完）
    返回 {h: {"rank": {"mean","icir"}, "pearson": {...}}}"""
    parts = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    plist = [str(p) for p in parts]
    con = duckdb.connect()
    con.execute(f"ATTACH '{DB}' AS maindb (READ_ONLY)")
    out: dict = {}
    for h in horizons:
        q = f"""
        WITH f AS (SELECT date, code, value FROM read_parquet({plist})
                   WHERE date BETWEEN DATE '{start}' AND DATE '{end}'),
        px AS (SELECT date, code, close * adj_factor AS c FROM maindb.kline_daily),
        base AS (
            SELECT f.date AS date, f.value AS v,
                   LEAD(px.c, {h}) OVER (PARTITION BY f.code ORDER BY f.date)
                       / px.c - 1 AS fw
            FROM f JOIN px ON f.date = px.date AND f.code = px.code),
        b AS (SELECT date, v, fw FROM base WHERE fw IS NOT NULL),
        r AS (SELECT date, v, fw,
                     RANK() OVER (PARTITION BY date ORDER BY v) AS rv,
                     RANK() OVER (PARTITION BY date ORDER BY fw) AS rf
              FROM b)
        SELECT date, corr(v, fw) AS pic, corr(rv, rf) AS ric
        FROM r GROUP BY date HAVING COUNT(*) >= 30 ORDER BY date
        """
        d = con.execute(q).df()
        if d.empty:
            out[h] = None
            continue
        def _s(col):
            s = d[col].dropna()
            if s.empty or s.std() == 0:
                return {"mean": None, "icir": None}
            return {"mean": round(float(s.mean()), 4),
                    "icir": round(float(s.mean() / s.std()), 3)}
        out[h] = {"rank": _s("ric"), "pearson": _s("pic"),
                  "n_days": int(len(d))}
    con.execute("DETACH maindb")
    con.close()
    return out


def style_exposure(name: str, style: str, start: str, end: str) -> dict:
    ap = [str(p) for p in sorted((FACTOR_DIR / name).glob("part-*.parquet"))]
    sp = [str(p) for p in sorted((FACTOR_DIR / style).glob("part-*.parquet"))]
    con = duckdb.connect()
    q = f"""
    WITH a AS (SELECT date, code, value AS av FROM read_parquet({ap})
               WHERE date BETWEEN DATE '{start}' AND DATE '{end}'),
    s AS (SELECT date, code, value AS sv FROM read_parquet({sp})
          WHERE date BETWEEN DATE '{start}' AND DATE '{end}'),
    j AS (
        SELECT a.date,
               RANK() OVER (PARTITION BY a.date ORDER BY a.av) AS ra,
               RANK() OVER (PARTITION BY a.date ORDER BY s.sv) AS rs
        FROM a JOIN s ON a.date = s.date AND a.code = s.code)
    SELECT date, corr(ra, rs) AS rho, COUNT(*) AS n FROM j
    GROUP BY date
    """
    d = con.execute(q).df()
    con.close()
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date")
    yearly = d.groupby(d.index.year)["rho"].mean().round(3)
    return {"all": round(float(d["rho"].mean()), 3),
            "n_days": int(len(d)),
            "yearly": {int(y): round(float(v), 3) for y, v in yearly.items()}}


def crowding_scan(name: str, n_days: int = 60) -> dict:
    """库内同质信号：最近 n_days 个交易日的逐日截面相关（pearson，n>=1000/日）"""
    parts = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    plist = [str(p) for p in parts]
    con = duckdb.connect()
    dates = [r[0] for r in con.execute(
        f"SELECT DISTINCT date FROM read_parquet({plist}) "
        f"ORDER BY date DESC LIMIT {n_days}").fetchall()]
    crowd_start = str(dates[-1])
    factor_names = sorted(d.name for d in FACTOR_DIR.iterdir()
                          if d.is_dir() and d.name != name)
    parts_map = {n: FACTOR_DIR / n / "part-2026.parquet" for n in factor_names}
    parts_map = {n: p for n, p in parts_map.items() if p.exists()}
    # 非当年窗口（回测历史区间）退回全分区读取
    if not str(dates[0]).startswith("2026"):
        parts_map = {}
        for n in factor_names:
            ps = sorted((FACTOR_DIR / n).glob("part-*.parquet"))
            if ps:
                parts_map[n] = None
    if parts_map and None in set(parts_map.values()):
        union_sql = " UNION ALL\n".join(
            f"SELECT date, code, value AS v, '{n}' AS fname FROM read_parquet("
            f"{[str(p) for p in sorted((FACTOR_DIR / n).glob('part-*.parquet'))]}) "
            f"WHERE date >= DATE '{crowd_start}'" for n in factor_names)
    else:
        union_sql = " UNION ALL\n".join(
            f"SELECT date, code, value AS v, '{n}' AS fname FROM read_parquet('{p}') "
            f"WHERE date >= DATE '{crowd_start}'" for n, p in parts_map.items())
    q_crowd = f"""
    WITH pool AS ({union_sql}),
    a AS (SELECT date, code, value AS av FROM read_parquet({plist})
          WHERE date >= DATE '{crowd_start}'),
    j AS (SELECT pool.date, pool.fname, pool.v, a.av
          FROM pool JOIN a ON pool.date = a.date AND pool.code = a.code),
    per_day AS (
        SELECT date, fname, corr(v, av) AS rho, COUNT(*) AS n
        FROM j GROUP BY date, fname)
    SELECT fname, AVG(rho) AS mean_rho, COUNT(*) AS n_days
    FROM per_day WHERE n >= 1000
    GROUP BY fname ORDER BY mean_rho DESC
    """
    crowd = con.execute(q_crowd).df()
    con.close()
    crowd["mean_rho"] = crowd["mean_rho"].round(3)
    return {
        "window": crowd_start,
        "n_factors_scanned": int(len(crowd)),
        "n_rho_gt_07": int((crowd["mean_rho"].abs() > 0.7).sum()),
        "n_rho_gt_05": int((crowd["mean_rho"].abs() > 0.5).sum()),
        "top_positive": [{"factor": r[0], "rho": round(float(r[1]), 3)}
                         for r in crowd.head(5).values.tolist()],
        "top_negative": [{"factor": r[0], "rho": round(float(r[1]), 3)}
                         for r in crowd.sort_values("mean_rho").head(5).values.tolist()],
    }


def small_cap_exposure(name: str, start: str, end: str) -> dict:
    ap = [str(p) for p in sorted((FACTOR_DIR / name).glob("part-*.parquet"))]
    szp = [str(p) for p in sorted((FACTOR_DIR / "size").glob("part-*.parquet"))]
    con = duckdb.connect()
    q = f"""
    WITH f AS (SELECT date, code, value FROM read_parquet({ap})
               WHERE date BETWEEN DATE '{start}' AND DATE '{end}'),
    sz AS (SELECT date, code, value FROM read_parquet({szp})
           WHERE date BETWEEN DATE '{start}' AND DATE '{end}'),
    j AS (
        SELECT f.date, f.value AS v, sz.value AS sv,
               NTILE(5) OVER (PARTITION BY f.date ORDER BY f.value) AS q,
               PERCENT_RANK() OVER (PARTITION BY f.date ORDER BY sz.value) AS spct
        FROM f JOIN sz ON f.date = sz.date AND f.code = sz.code)
    SELECT year(date) AS y,
           quantile_cont(spct, 0.5) FILTER (WHERE q = 5) AS q5_size_pct,
           quantile_cont(spct, 0.5) AS all_size_pct,
           COUNT(*) AS n
    FROM j GROUP BY year(date) ORDER BY y
    """
    small = con.execute(q).df()
    con.close()
    return {"yearly": [{"year": int(r["y"]),
                        "q5_size_pct": round(float(r["q5_size_pct"]), 3),
                        "all_size_pct": round(float(r["all_size_pct"]), 3)}
                       for _, r in small.iterrows()]}


def latest_top(name: str) -> dict:
    parts = sorted((FACTOR_DIR / name).glob("part-*.parquet"))
    plist = [str(p) for p in parts]
    con = duckdb.connect()
    last_day = con.execute(
        f"SELECT max(date) AS d FROM read_parquet({plist})").df()["d"][0]
    top = con.execute(f"""
        SELECT value, code FROM read_parquet({plist})
        WHERE date = DATE '{last_day}' ORDER BY value DESC LIMIT 10
    """).df()
    con.close()
    return {"date": f"{pd.to_datetime(last_day):%Y-%m-%d}",
            "top": [{"code": r["code"], "value": round(float(r["value"]), 4)}
                    for _, r in top.iterrows()]}


def group_stats(r: pd.Series, h: int) -> dict:
    daily = (r / h).dropna()
    ann = float(daily.mean() * 252)
    vol = float(daily.std() * np.sqrt(252))
    nav = (1 + daily).cumprod()
    mdd = float((nav / nav.cummax() - 1).min())
    mon = daily.groupby([daily.index.year, daily.index.month]).apply(
        lambda s: (1 + s).prod() - 1)
    return {"ann_ret": round(ann, 4), "ann_vol": round(vol, 4),
            "sharpe": round(ann / vol, 2) if vol else None,
            "mdd": round(mdd, 3),
            "mon_win": round(float((mon > 0).mean()), 3),
            "mean_fwd_bp": round(float(r.mean()) * 1e4, 1)}


# ─────────────────────────── 主流程 ───────────────────────────
def main() -> None:
    p = argparse.ArgumentParser(description="因子看板标准模板构建器")
    p.add_argument("--name", required=True, help="因子名（因子湖目录名）")
    p.add_argument("--start", default="2023-01-01")
    p.add_argument("--end", default="2026-09-11")
    p.add_argument("--tags", default="", help="逗号分隔标签")
    p.add_argument("--h-ic", type=int, default=5, help="IC 主口径 horizon（默认 5）")
    p.add_argument("--h-grp", type=int, default=20, help="分组收益 horizon（默认 20）")
    a = p.parse_args()
    name, start, end, H_IC, H_GRP = a.name, a.start, a.end, a.h_ic, a.h_grp
    tags = [t.strip() for t in a.tags.split(",") if t.strip()]
    t0 = time.time()

    if not (FACTOR_DIR / name).exists():
        raise FileNotFoundError(f"因子湖无 {name}（{FACTOR_DIR}）")

    # ── 1. 基础信息 ──
    aud = audit_info(name)
    fdr = fdr_info(name)
    reg = registry_info(name)
    n_pool = len([d for d in FACTOR_DIR.iterdir() if d.is_dir()])
    n_audited = len(list((ROOT / "data" / "lake" / "factor_audit").glob("*.json")))

    # ── 2. IC 统计（主口径 + 调仓口径）──
    daily5, _, _ = factor_daily_metrics(name, H_IC, start, end)
    daily20, yearly, extra20 = factor_daily_metrics(name, H_GRP, start, end)
    nav_dates = [f"{d:%Y-%m-%d}" for d in daily20["date"]]
    daily5 = daily5.set_index("date")
    daily20 = daily20.set_index("date")
    ic5 = daily5["ic"].dropna()
    rho = float(ic5.autocorr(1))
    n = len(ic5)
    n_eff = n * (1 - rho) / (1 + rho)
    icir5 = float(ic5.mean() / ic5.std())
    t_adj = icir5 * np.sqrt(n_eff)
    # 双类型 IC 矩阵（Pearson vs Rank × h=1/5/20），兼供概览 Pearson 卡
    icmx = ic_type_matrix(name, start, end)
    if icmx.get(H_IC) and icmx[H_IC]["pearson"]["mean"] is not None:
        pear5 = icmx[H_IC]["pearson"]["mean"]
    else:
        pear5 = pearson_ic(name, H_IC, start, end)
    monthly5 = (ic5.groupby([ic5.index.year, ic5.index.month]).mean()
                .unstack().round(4))

    # ── 3. 结构：谱 / 十分位 / 正交化 / 核心相关 ──
    horizons = [1, 3, 5, 10, 20, 40, 60, 120]
    decay, dmeta = factor_horizon_structure(name, horizons, start, end)
    decile = factor_decile(name, H_GRP, start, end)
    resid = factor_residual_ic(name, H_GRP, start, end)
    corr = factor_core_corr(name, start, end)

    # ── 4. 风格 / 拥挤 / 小票暴露 / 最新 ──
    style_exp = {k: style_exposure(name, k, start, end) for k in STYLES}
    crowd = crowding_scan(name)
    small = small_cap_exposure(name, start, end)
    latest = latest_top(name)

    # ── 5. 分组收益（调仓口径）──
    qcols = [f"q{i}_ret" for i in range(1, 6)]
    groups = {f"Q{i}": group_stats(daily20[f"q{i}_ret"], H_GRP)
              for i in range(1, 6)}
    ls = daily20["q5_ret"] - daily20["q1_ret"]
    groups["LS"] = group_stats(ls, H_GRP)
    navs = {}
    for i in range(1, 6):
        d = (daily20[f"q{i}_ret"] / H_GRP).dropna()
        navs[f"Q{i}"] = [round(float(v), 4) for v in (1 + d).cumprod()]
    navs["LS"] = [round(float(v), 4)
                  for v in (1 + (ls / H_GRP).dropna()).cumprod()]
    best_q = 1 + int(np.argmax([groups[f"Q{i}"]["mean_fwd_bp"]
                                for i in range(1, 6)]))

    # ── 6. 红绿灯与标记（与 run_eval.eval_factor 同口径）──
    verdict = aud.get("verdict", "未审计") if aud else "未审计"
    dirn = (aud.get("checks", {}).get("ic", {}).get("direction", 1)
            if aud else 1)
    q = fdr.get("q") if fdr else None
    t_lib = fdr.get("t") if fdr else None
    flags: list[str] = []
    red = verdict == "FAIL"
    if red:
        flags.append("PIT/结构审计 FAIL")
    elif aud is None:
        flags.append("未审计（无 factor_audit JSON）")
    elif verdict == "WARN":
        flags.append("审计 WARN")
    if q is None:
        flags.append("无 FDR 记录（先跑 fdr_multitest.py）")
    elif q >= 0.25:
        flags.append(f"FDR q={q} 统计证据弱")
    if "未登记" in reg["rationale"]:
        flags.append("经济机制未登记")
    n_neg = int((yearly["ic_mean"] * dirn < 0).sum())
    if n_neg >= 2:
        flags.append(f"{n_neg}/{len(yearly)} 年 IC 反号")
    if abs(extra20["roll_icir_last"]) < 0.05:
        flags.append(f"近 12M 滚动 ICIR {extra20['roll_icir_last']}（信号衰减）")
    redundants = []
    if corr is not None and len(corr):
        redundants = corr[corr["rho"].abs() > 0.7]
        for _, w in redundants.iterrows():
            flags.append(f"与核心因子 {w['core']} 相关 {w['rho']:+.2f}"
                         "（|ρ|>0.7 冗余）")
    resid_weak = False
    if resid is not None and abs(resid["raw_ic"]) > 0.03 \
            and abs(resid["resid_ic"]) < 0.5 * abs(resid["raw_ic"]):
        resid_weak = True
        flags.append(f"正交化后增量微弱（残差 IC {resid['resid_ic']:+.4f}"
                     f" vs 原始 {resid['raw_ic']:+.4f}）")
    light = "🔴" if red else ("🟡" if len(flags) >= 2 else "🟢")

    # ── 7. DATA 对象 ──
    data = {
        "name": name, "h_ic": H_IC, "h_grp": H_GRP, "best_q": best_q,
        "ic5": {
            "mean": round(float(ic5.mean()), 4), "std": round(float(ic5.std()), 4),
            "icir": round(icir5, 3), "skew": round(float(ic5.skew()), 3),
            "kurt": round(float(ic5.kurt()), 3), "lag1_autocorr": round(rho, 3),
            "n_days": n, "n_eff": round(n_eff, 0), "t_adj": round(float(t_adj), 2),
            "p_ic_pos": round(float((ic5 > 0.02).mean()), 4),
            "p_ic_neg": round(float((ic5 < -0.02).mean()), 4),
            "win_rate": round(float((ic5 > 0).mean()), 4),
            "pearson_mean": round(pear5, 4),
        },
        "ic5_series": [round(float(v), 4) for v in ic5.values],
        "ic5_acf": [round(float(ic5.autocorr(k)), 3) for k in range(1, 11)],
        "monthly_ic5": {"years": [int(i) for i in monthly5.index],
                        "months": [int(c) for c in monthly5.columns],
                        "values": [[None if pd.isna(v) else round(float(v), 4)
                                    for v in row] for row in monthly5.values]},
        "decay": {"h": [int(h) for h in decay["h"]],
                  "ic": [round(float(v), 4) for v in decay["ic"]],
                  "icir": [round(float(v), 3) for v in decay["icir"]]},
        "ic_type_matrix": icmx,
        "decile_bp": decile["bp"],
        "groups": groups, "navs": navs,
        "nav_dates": nav_dates,
        "style_exposure": style_exp, "style_names": STYLES,
        "crowding": crowd, "small_cap_exposure": small, "latest": latest,
        "resid": ({"raw": resid["raw_ic"], "resid": resid["resid_ic"]}
                  if resid else None),
    }

    # ── 8. 文案（全部数据驱动）──
    I = data["ic5"]
    hl = (f"{dmeta['half_life']} 日" if dmeta["half_life"] is not None
          else f"&gt;{dmeta['max_h']} 日（测程内未衰减过半）")
    ic_h5 = float(decay.loc[decay["h"] == H_IC, "ic"].iloc[0]) if H_IC in list(decay["h"]) else I["mean"]
    ic_hg = float(decay.loc[decay["h"] == H_GRP, "ic"].iloc[0]) if H_GRP in list(decay["h"]) else extra20["ic_all"]
    if dmeta["half_life"] is None and dmeta["peak_h"] >= 40:
        decay_note = (f"横轴为<b>持有期长度</b>（信号预测的未来收益区间），非时间。"
                      f"峰值 |IC| {dmeta['peak_ic']:.4f} 出现在 h={dmeta['peak_h']}，"
                      f"半衰期 {hl}——<b>慢周期配置型信号</b>，{H_GRP} 日调仓不损耗预测力；"
                      f"主口径 h={H_IC} 处 IC {ic_h5:+.4f}，h={H_GRP} 达 {ic_hg:+.4f}。")
    else:
        hl2 = f"{dmeta['half_life']} 日" if dmeta["half_life"] is not None else f"&gt;{dmeta['max_h']} 日"
        decay_note = (f"横轴为<b>持有期长度</b>（非时间）。峰值 |IC| "
                      f"{dmeta['peak_ic']:.4f} 出现在 h={dmeta['peak_h']}，"
                      f"半衰期 {hl2}——调仓周期若显著超过半衰期，预测力在等待"
                      f"建仓时已耗散（换手下限解读）。主口径 h={H_IC} 处 "
                      f"IC {ic_h5:+.4f}。")
    if I["skew"] < -0.3:
        hist_note = ("左偏（负偏）= 存在 IC 深负的坏月份，均值被尾部拖累——"
                     "坏月份的定位见右下月度热力图。")
    elif I["skew"] > 0.3:
        hist_note = ("右偏 = 存在 IC 深正的好月份拉动均值，常态预测力弱于均值"
                     "表观——好月份的分布见右下月度热力图。")
    else:
        hist_note = "近似对称分布，无极端月份主导均值。"
    acf_note = (f"lag1 ρ={I['lag1_autocorr']:.3f}：{H_IC} 日 IC 序列相邻共享 "
                f"{H_IC-1}/{H_IC} 收益窗口。多重检验的 N_eff 折减即以此为据——"
                "不做折减时 276/311 因子\"显著\"，等于没筛。")
    monthly_note = "IC 深负/深正的极端月份一眼可见——是分布偏态的来源。"

    # IC 口径对比（Pearson vs Rank × h=1/5/20）
    def _mx(h):
        return icmx.get(h) or icmx.get(str(h))

    icmx_rows = []
    for h in (1, 5, 20):
        m = _mx(h)
        if not m or m["rank"]["mean"] is None:
            icmx_rows.append(f"<tr><td>h={h} 日</td>"
                             "<td>—</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>")
            continue
        r, p = m["rank"], m["pearson"]
        ratio = (f"{abs(r['mean']) / abs(p['mean']):.1f}×"
                 if p["mean"] else "—")
        def _c(v):
            return "pos" if (v or 0) > 0 else "neg"
        icmx_rows.append(
            f"<tr><td>h={h} 日</td>"
            f"<td class=\"{_c(r['mean'])}\">{r['mean']:+.4f}</td>"
            f"<td class=\"{_c(r['icir'])}\">{r['icir']:+.3f}</td>"
            f"<td class=\"{_c(p['mean'])}\">{p['mean']:+.4f}</td>"
            f"<td class=\"{_c(p['icir'])}\">{p['icir']:+.3f}</td>"
            f"<td>{ratio}</td></tr>")
    icmx_rows_html = "\n      " + "\n      ".join(icmx_rows)
    m5 = _mx(H_IC) or {}
    if m5 and m5["rank"]["mean"] and m5["pearson"]["mean"]:
        ratio5 = abs(m5["rank"]["mean"]) / abs(m5["pearson"]["mean"])
        if ratio5 > 1.5:
            icmx_note = (f"Rank IC 是 Pearson 的 {ratio5:.1f} 倍——预测力集中在"
                         "<b>排序结构</b>而非线性幅度，截面厚尾极值稀释了 Pearson。"
                         "主口径取 Rank 合理；Pearson 供横向对比。")
        elif ratio5 < 0.7:
            icmx_note = ("Pearson 反而高于 Rank（少见）——线性幅度携带的信息"
                         "多于排序结构，建议检查是否存在极值驱动的伪线性。")
        else:
            icmx_note = ("两口径接近（比值 "
                         f"{ratio5:.1f}×）——因子与远期收益近似线性，"
                         "Rank 稳健性优势不构成主要差异。")
    else:
        icmx_note = "双类型矩阵数据缺失。"
    m1 = _mx(1)
    if m1 and m1["rank"]["mean"] is not None:
        icmx_note += (f" h=1（次日收益）Rank IC {m1['rank']['mean']:+.4f}"
                      f"（ICIR {m1['rank']['icir']:+.3f}）"
                      "——短端即有效/需要等待建仓的判断依据。")

    se = style_exp
    order = sorted(se, key=lambda k: -abs(se[k]["all"]))
    dom, dom2 = order[0], order[1]
    dom_y = se[dom]["yearly"]
    y_first, y_last = min(dom_y), max(dom_y)
    drift_k = max(se, key=lambda k: (max(se[k]["yearly"].values())
                                     - min(se[k]["yearly"].values()))
                  if len(se[k]["yearly"]) > 1 else 0)
    if abs(se[dom]["all"]) > 0.5:
        if dom == "size":
            interp = (f"该因子本质上是<b>{'小' if se[dom]['all'] < 0 else '大'}"
                      "市值风格的结构化表达</b>——做多组合 = 事实上"
                      f"{'做空' if se[dom]['all'] < 0 else '做多'}市值，"
                      "风格中性化检验为入库必做项。")
        else:
            interp = (f"该因子收益中<b>{STYLES[dom]}</b>成分占主导，"
                      "入库前须对该风格做正交化增量检验。")
    else:
        interp = "无主导风格暴露——风格因子之外的结构化信号。"
    style_note = (f"暴露最强的是 {STYLES[dom]}（ρ={se[dom]['all']:+.3f}，"
                  f"年度区间 [{y_first:+.3f}, {y_last:+.3f}]），第二位 "
                  f"{STYLES[dom2]} ρ={se[dom2]['all']:+.3f}。{interp}")
    dv = se[drift_k]["yearly"]
    style_drift_note = (f"{STYLES[drift_k]} 暴露逐年变动最大"
                        f"（{min(dv.values()):+.3f} → {max(dv.values()):+.3f}）"
                        "——风格身份随制度切换，依赖该暴露的收益须做风格"
                        "正交化检验。")

    bp = decile["bp"]
    if bp[9] >= bp[0]:
        decile_note = (f"与左侧五分位同源、加细到十组：D9→D10 的增量"
                       f"（{bp[8]:+.0f}→{bp[9]:+.0f}bp）才是组合真正买入的"
                       f"部分，五分位看不出尾部结构。多空（D10−D1）= "
                       f"{decile['spread_bp']:+.0f}bp/{H_GRP}日。")
    else:
        decile_note = (f"与左侧五分位同源、加细到十组：该因子方向下买入端在"
                       f"低分位，D1→D2 的增量（{bp[0]:+.0f}→{bp[1]:+.0f}bp）"
                       f"是组合真正买入的部分。多空（D1−D10）= "
                       f"{-decile['spread_bp']:+.0f}bp/{H_GRP}日。")

    ls_rows_html, yr_rows_html = [], []
    sign = 1 if extra20["ic_all"] >= 0 else -1
    for _, row in yearly.iterrows():
        s = row["long_share"]
        if pd.notna(s) and s:
            share_txt = f"{s:.0%}" + ("（空头腿也涨）" if s > 1.5 else "")
        elif row["q5_ret"] < 0 and row["ls_spread"] < 0:
            share_txt = "—（多空同负）"
        else:
            share_txt = "—"
        ls_rows_html.append(
            f"<tr><td>{int(row['year'])}</td>"
            f"<td class=\"{'pos' if row['q5_ret'] > 0 else 'neg'}\">"
            f"{row['q5_ret']*1e4:+.0f}bp</td>"
            f"<td class=\"{'pos' if row['q1_ret'] > 0 else 'neg'}\">"
            f"{row['q1_ret']*1e4:+.0f}bp</td>"
            f"<td class=\"{'pos' if row['ls_spread'] > 0 else 'neg'}\">"
            f"{row['ls_spread']*1e4:+.0f}</td><td>{share_txt}</td></tr>")
        yr_rows_html.append(
            f"<tr><td>{int(row['year'])}</td><td>{int(row['n_days'])}</td>"
            f"<td class=\"{'pos' if row['ic_mean'] > 0 else 'neg'}\">"
            f"{row['ic_mean']:+.4f}</td><td>{row['icir']:+.3f}</td>"
            f"<td>{row['win_rate']:.0%}</td>"
            + "".join(f"<td class=\"{'pos' if row[c] > 0 else 'neg'}\">"
                      f"{row[c]*1e4:+.0f}</td>" for c in qcols)
            + f"<td class=\"{'pos' if row['ls_spread'] > 0 else 'neg'}\">"
              f"{row['ls_spread']*1e4:+.0f}</td></tr>")
    ls_table_html = ("<table style=\"margin-top:6px\"><tr><th>年</th>"
                     "<th>多头腿 Q5</th><th>空头腿 Q1</th><th>多空</th>"
                     "<th>多头占比</th></tr>" + "".join(ls_rows_html) + "</table>")
    yearly_table_html = ("<table style=\"margin-top:8px\"><tr><th>年</th>"
                         "<th>天数</th><th>IC</th><th>ICIR</th><th>胜率</th>"
                         "<th>Q1</th><th>Q2</th><th>Q3</th><th>Q4</th>"
                         "<th>Q5</th><th>多空</th></tr>"
                         + "".join(yr_rows_html) + "</table>")
    ls_note = ("A 股无低成本做空：预测力集中在空头腿 = 不可收割。多头占比 "
               "&gt;100% = 空头腿自身为正，多空价差全部由多头腿贡献——"
               "直接回答\"IC 好的钱到底能不能赚\"。")
    bad_years = [int(r["year"]) for _, r in yearly.iterrows()
                 if (r["q5_ret"] - r["q1_ret"]) * sign < 0]
    if bad_years:
        yearly_note = (f"{', '.join(map(str, bad_years))} 年多空腿与信号方向相反"
                       f"——信号衰减的年度级证据，与滚动 ICIR 区间 "
                       f"[{extra20['roll_icir_min']}, {extra20['roll_icir_max']}]"
                       " 互相印证。")
    else:
        yearly_note = "各年度多空腿均与信号方向一致，无年度级反号。"

    if resid is not None:
        keep_pct = abs(resid["resid_ic"]) / abs(resid["raw_ic"]) * 100 \
            if resid["raw_ic"] else 0
        resid_note = (f"对 {' + '.join(resid['cores'])} 逐日截面 OLS 取残差后的 "
                      f"rank IC（h={H_GRP}）：原始 {resid['raw_ic']:+.4f} → "
                      f"残差 {resid['resid_ic']:+.4f}，<b>保留 {keep_pct:.0f}% "
                      f"增量</b>（ICIR {resid['raw_icir']}→{resid['resid_icir']}）。"
                      + ("残差 IC 显著缩水——与生产因子共有成分高，入库价值"
                         "须按边际贡献重排。"
                         if keep_pct < 50 else "残差 IC 保持——独立信息源，"
                         "组合分散价值大。"))
    else:
        resid_note = "无可对齐的核心生产因子，正交化增量未计算。"

    if corr is not None and len(corr):
        corr_rows = "".join(
            f"<tr><td>{row['core']}</td>"
            f"<td class=\"{'pos' if row['rho'] > 0 else 'neg'}\">"
            f"{row['rho']:+.3f}</td><td>{int(row['n_days'])}</td>"
            + ("<td class=\"warn\">⚠️ 冗余（|ρ|&gt;0.7）</td>"
               if abs(row["rho"]) > 0.7 else "<td class=\"ok\">独立</td>")
            + "</tr>" for _, row in corr.iterrows())
    else:
        corr_rows = "<tr><td colspan=\"4\">无数据</td></tr>"
    corr_note = ("与核心生产因子的信息重叠度——与左图正交化结论交叉验证。"
                 if corr is not None and len(corr) else "")

    cr, sc = crowd, small["yearly"]
    crowd_note = (f"库内 {cr['n_factors_scanned']} 个可扫因子中 "
                  f"|ρ|&gt;0.7 的 {cr['n_rho_gt_07']} 个、|ρ|&gt;0.5 的 "
                  f"{cr['n_rho_gt_05']} 个"
                  f"（{cr['n_rho_gt_05']/max(cr['n_factors_scanned'],1)*100:.0f}%）"
                  "——正相关 = 多头拥挤同一批票；负相关高 = 因子本质是同一"
                  "信号的镜像。完整拥挤度评估需公募持仓/两融等外部数据，"
                  "此处为库内代理。")
    if cr["n_rho_gt_07"] >= 10 or sc[-1]["q5_size_pct"] < 0.15:
        crowd_flag, crowd_flag_note = "偏高", "同质信号多或买入组深居小票分位"
    elif cr["n_rho_gt_05"] >= 5 or sc[-1]["q5_size_pct"] < 0.3:
        crowd_flag, crowd_flag_note = "中", "库内代理口径，须结合容量测算"
    else:
        crowd_flag, crowd_flag_note = "低", "库内代理口径"
    if sc[-1]["q5_size_pct"] < 0.3:
        small_note = (f"买入组全期在第 {sc[-1]['q5_size_pct']:.0%} 分位附近"
                      f"（{sc[0]['year']} 年 {sc[0]['q5_size_pct']:.3f} → "
                      f"{sc[-1]['year']} 年 {sc[-1]['q5_size_pct']:.3f}）——"
                      "深度小票暴露是该因子拥挤风险的结构性来源："
                      "2024-02 式微盘流动性事件是本因子的尾部风险事件。")
    else:
        small_note = (f"买入组市值分布（{sc[-1]['year']} 年 size 分位中位 "
                      f"{sc[-1]['q5_size_pct']:.2f}，全市场中位 0.50）接近"
                      "市场整体——小票暴露不是该因子的主要风险来源。")

    # 判定表
    def _cell(ok: str) -> str:
        return {"✅": "ok", "🟡": "warn", "❌": "warn"}[ok]

    v_rows = [
        ("PIT / 结构审计（时点信息合规）", verdict,
         "✅" if verdict == "PASS" else ("❌" if red else "🟡")),
        (f"现场重算 IC{H_IC} / ICIR{H_IC}（主口径）",
         f"{I['mean']:+.4f} / {I['icir']:+.3f}（{I['n_days']} 交易日，"
         f"胜率 {I['win_rate']:.0%}）",
         "✅" if abs(I["icir"]) > 0.1 else "🟡"),
        ("FDR q（BH + ρ 自相关折减，库级 h=20）",
         f"{q}（t_adj {t_lib}，HLZ 门槛 3.0）" if q is not None else "无记录",
         ("✅" if q < 0.05 else ("🟡" if q < 0.25 else "❌"))
         if q is not None else "❌"),
        (f"分组单调性（五分位 / 十分位，h={H_GRP}）",
         f"{extra20['monotonicity']} / {decile['mono']}",
         "✅" if abs(extra20["monotonicity"]) > 0.8 else "🟡"),
        ("近 12M 滚动 ICIR",
         f"{extra20['roll_icir_last']}（区间 "
         f"[{extra20['roll_icir_min']}, {extra20['roll_icir_max']}]）",
         "✅" if abs(extra20["roll_icir_last"]) > 0.15 else "🟡"),
        ("经济机制登记",
         reg["rationale"] if "未登记" not in reg["rationale"]
         else "未登记",
         "✅" if "未登记" not in reg["rationale"] else "❌"),
        ("年度 IC 反号", f"{n_neg}/{len(yearly)} 年",
         "✅" if n_neg < 2 else "🟡"),
        ("核心因子冗余",
         " ｜ ".join(f"{w['core']} {w['rho']:+.2f}"
                    for _, w in redundants.iterrows())
         if len(redundants) else "无 |ρ|&gt;0.7 的核心因子",
         "🟡" if len(redundants) else "✅"),
        ("正交化增量（对生产因子）",
         (f"残差 IC {resid['resid_ic']:+.4f} vs 原始 {resid['raw_ic']:+.4f}"
          if resid else "未计算"),
         ("🟡" if resid_weak else "✅") if resid else "🟡"),
    ]
    verdict_table_html = ("<table style=\"margin-top:12px\">"
                          "<tr><th>检验项</th><th>结果</th><th>判定</th></tr>"
                          + "".join(
                              f"<tr><td>{t}</td><td>{r}</td>"
                              f"<td class=\"{_cell(o)}\">{o}</td></tr>"
                              for t, r, o in v_rows) + "</table>")

    if light == "🟢":
        verdict_summary = "全部检验通过，无风险标记"
    else:
        verdict_summary = "；".join(flags)

    half_life_disp = (f"{dmeta['half_life']}" if dmeta["half_life"] is not None
                      else f"&gt;{dmeta['max_h']}")

    # ── 9. 渲染 ──
    html = build_html(data, {
        "name": name, "desc": reg["description"] or name,
        "category": reg["category"], "tags": tags,
        "rationale": reg["rationale"],
        "start": start, "end": end, "latest_date": latest["date"],
        "n_pool": n_pool, "n_audited": n_audited,
        "verdict": verdict, "q": q, "t_lib": t_lib,
        "half_life": half_life_disp,
        "light": light, "verdict_summary": verdict_summary,
        "verdict_table": verdict_table_html,
        "decay_note": decay_note, "hist_note": hist_note,
        "acf_note": acf_note, "monthly_note": monthly_note,
        "icmx_rows": icmx_rows_html, "icmx_note": icmx_note,
        "style_note": style_note, "style_drift_note": style_drift_note,
        "decile_note": decile_note, "ls_table": ls_table_html,
        "ls_note": ls_note, "yearly_table": yearly_table_html,
        "yearly_note": yearly_note, "resid_note": resid_note,
        "corr_rows": corr_rows, "corr_note": corr_note,
        "crowd_note": crowd_note, "crowd_flag": crowd_flag,
        "crowd_flag_note": crowd_flag_note, "small_note": small_note,
        "elapsed": round(time.time() - t0, 1),
    })
    DOCS.mkdir(exist_ok=True)
    out = DOCS / f"factor_center_{name}.html"
    out.write_text(html, encoding="utf-8")

    # ── 10. 索引登记 ──
    idx = (json.loads(INDEX.read_text(encoding="utf-8"))
           if INDEX.exists() else [])
    ts = datetime.now()
    idx.append({
        "id": f"dashboard:{name}:{ts:%Y%m%d%H%M}",
        "type": "dashboard", "name": name,
        "date": f"{ts:%Y-%m-%d}", "generated_at": ts.isoformat(timespec="seconds"),
        "window": f"{start}~{end}", "tags": tags, "light": light,
        "flags": flags, "file": str(out.relative_to(ROOT)).replace("\\", "/"),
        "template": "dashboard-v3",
        "summary": (f"IC{H_IC} {I['mean']:+.4f} t_adj {I['t_adj']:.2f} "
                    f"灯 {light} 看板 {out.name}"),
    })
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=1),
                     encoding="utf-8")

    print(f"[done] {light} {name}: IC{H_IC} {I['mean']:+.4f} "
          f"t_adj {I['t_adj']:.2f} q={q}")
    for f in flags:
        print(f"  ⚠️ {f}")
    print(f"[dashboard] {out}  ({out.stat().st_size:,} bytes)")
    print(f"[index] {len(idx)} 条（type=dashboard 可检索）")


def build_html(D: dict, X: dict) -> str:
    import json as _json
    tags_html = "".join(f'<span class="chip">{t}</span>' for t in X["tags"])
    TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>因子看板 · __NAME__</title>
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
.g2{grid-template-columns:1fr 1fr}.g3{grid-template-columns:1fr 1fr 1fr}
.g4{grid-template-columns:repeat(4,1fr)}
@media(max-width:900px){.g2,.g3,.g4{grid-template-columns:1fr}}
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
footer{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--sub);font-size:11.5px}
</style>
</head>
<body>
<div class="wrap">

<header>
  <h1>因子看板 · __NAME__<span class="badge">主口径 IC__HIC__</span></h1>
  <div class="meta">__DESC__｜全A · __START__ ~ __END__｜IC 主口径 = __HIC__ 日 rank IC｜分组收益 = __HGRP__ 日远期（调仓周期）｜数据更新至 __LATEST__</div>
</header>

<h2>概览指标</h2>
<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px">
    <div><b style="font-size:16px">__NAME__</b>
      <span class="chip">__CATEGORY__</span>__TAGS__
      <span class="chip">机制登记：__RATIONALE__</span></div>
    <div style="color:var(--sub);font-size:11.5px">因子池 __NPOOL__ 个（已审计 __NAUDITED__）</div>
  </div>
  <div class="grid g4" style="margin-top:12px">
    <div class="metric"><div class="k">Rank_IC（__HIC__ 日，主口径）</div><div class="v __IC5_MEAN_CLS__">__IC5_MEAN__</div></div>
    <div class="metric"><div class="k">IC_IR（__HIC__ 日）</div><div class="v">__IC5_ICIR__</div></div>
    <div class="metric"><div class="k">IC_STD（__HIC__ 日）</div><div class="v">__IC5_STD__</div></div>
    <div class="metric"><div class="k">IC_MEAN（Pearson，__HIC__ 日）</div><div class="v">__IC5_PEAR__</div></div>
  </div>
  <div class="grid g4" style="margin-top:8px">
    <div class="metric"><div class="k">FDR q（BH + 自相关折减，库级 h=20 快照）</div><div class="v">__FDR_Q__</div><div class="s">__NPOOL__ 因子多重检验校正</div></div>
    <div class="metric"><div class="k">t_adj（__HIC__ 日本窗实测）</div><div class="v">__IC5_TADJ__</div><div class="s">N_eff=__IC5_NEFF__（lag1 ρ=__IC5_RHO__）__T_LIB__</div></div>
    <div class="metric"><div class="k">PIT 审计</div><div class="v">__PIT_CLS__</div><div class="s">时点信息合规</div></div>
    <div class="metric"><div class="k">IC 半衰期</div><div class="v">__HALF__</div><div class="s">信号自然换手下限</div></div>
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

<h2>IC 分析（__HIC__ 日主口径）</h2>
<div class="desc">多周期结构 · 分布形态 · 序列自相关 · 月度时变性</div>

<div class="grid g2">
  <div class="card"><b>多周期 IC / ICIR 谱（持有期 h = 1 ~ 120 日，h=__HIC__ 为主口径）</b>
    <div id="chart_decay"></div>
    <div class="note">__DECAY_NOTE__</div>
  </div>
  <div class="card"><b>IC__HIC__ 分布（skew = __IC5_SKEW__，kurt = __IC5_KURT__）</b>
    <div id="chart_hist"></div>
    <div class="note">__HIST_NOTE__</div>
  </div>
  <div class="card"><b>IC__HIC__ 自相关（lag 1 ~ 10）</b>
    <div id="chart_acf"></div>
    <div class="note">__ACF_NOTE__</div>
  </div>
  <div class="card"><b>月度 IC__HIC__ 热力图</b>
    <div style="overflow-x:auto"><table class="heatmap" id="tbl_monthly"></table></div>
    <div class="note">__MONTHLY_NOTE__</div>
  </div>
</div>

<div class="grid g4" style="margin-top:12px">
  <div class="metric"><div class="k">P(IC__HIC__ &gt; 0.02)</div><div class="v">__IC5_PPOS__</div></div>
  <div class="metric"><div class="k">P(IC__HIC__ &lt; −0.02)</div><div class="v">__IC5_PNEG__</div></div>
  <div class="metric"><div class="k">IC__HIC__ 胜率</div><div class="v">__IC5_WIN__</div></div>
  <div class="metric"><div class="k">p-value（N_eff 折减后）</div><div class="v">__IC5_P__</div><div class="s">t_adj=__IC5_TADJ__</div></div>
</div>

<div class="card" style="margin-top:12px"><b>IC 口径对比（Pearson vs Rank × 持有期 h = 1 / 5 / 20 日）</b>
  <table style="margin-top:8px">
    <tr><th>持有期</th><th>Rank IC</th><th>Rank ICIR</th><th>Pearson IC</th><th>Pearson ICIR</th><th>Rank/Pearson</th></tr>
__ICMX_ROWS__
  </table>
  <div class="note">__ICMX_NOTE__</div>
</div>

<h2>风格分解</h2>
<div class="desc">该因子本质上是什么风格的组合：与 6 个风格代表因子的逐日截面 spearman 相关（全期 + 年度演变）</div>

<div class="grid g2">
  <div class="card"><b>风格暴露（全期截面 spearman 均值 ρ）</b>
    <div id="chart_style"></div>
    <div class="note">__STYLE_NOTE__</div>
  </div>
  <div class="card"><b>风格暴露年度演变</b>
    <table id="tbl_style" style="margin-top:6px"></table>
    <div class="note">__STYLE_DRIFT_NOTE__</div>
  </div>
</div>

<h2>分组分析（__HGRP__ 日远期 · 调仓周期口径）</h2>
<div class="desc">五分位看整体单调结构，十分位看尾部集中度（组合实际买入的是最右一组）；口径 = __HGRP__ 日远期收益逐日摊薄（非真实可交易组合，终审须组合层 A/B）</div>

<div class="card"><b>分组收益（五分位 + 多空）</b>
  <table id="tbl_groups" style="margin-top:8px"></table>
</div>

<div class="grid g2" style="margin-top:12px">
  <div class="card"><b>分组净值曲线（__START__ ~ __END__）</b>
    <div id="chart_nav"></div>
    <div class="note">Q1~Q5 = 因子值五分位组逐日摊薄净值；金线 = 多空（Q5−Q1）。</div>
  </div>
  <div class="card"><b>十分位分组 · 尾部集中度（全期均值，bp/__HGRP__日）</b>
    <div id="chart_decile"></div>
    <div class="note">__DECILE_NOTE__</div>
  </div>
  <div class="card"><b>多空腿可收割性（A 股做空约束）</b>
__LS_TABLE__
    <div class="note">__LS_NOTE__</div>
  </div>
  <div class="card"><b>年度分解（含五分位组收益，bp/__HGRP__日）</b>
__YEARLY_TABLE__
    <div class="note">__YEARLY_NOTE__</div>
  </div>
</div>

<h2>信息增量</h2>
<div class="desc">该因子在现有生产因子集之外还能贡献多少独立信息</div>

<div class="grid g2">
  <div class="card"><b>正交化增量（对生产因子的边际贡献）</b>
    <div id="chart_resid"></div>
    <div class="note">__RESID_NOTE__</div>
  </div>
  <div class="card"><b>与核心因子截面相关（最近 250 日 spearman 均值）</b>
    <table style="margin-top:6px">
      <tr><th>核心因子</th><th>ρ</th><th>天数</th><th>判定</th></tr>
__CORR_ROWS__
    </table>
    <div class="note">__CORR_NOTE__</div>
  </div>
</div>

<h2>拥挤度</h2>
<div class="desc">该信号赛道有多挤：库内同质信号扫描 + 买入组的小票暴露（拥挤风险的结构性来源）</div>

<div class="grid g4">
  <div class="metric"><div class="k">同质信号 |ρ| &gt; 0.7</div><div class="v" style="color:var(--green)">__CROWD_N07__</div><div class="s">库内 __CROWD_SCANNED__ 个因子（近 60 日截面相关）</div></div>
  <div class="metric"><div class="k">同质信号 |ρ| &gt; 0.5</div><div class="v" style="color:var(--gold)">__CROWD_N05__</div><div class="s">占比 __CROWD_PCT05__</div></div>
  <div class="metric"><div class="k">Q5 组 size 分位中位（最新年）</div><div class="v" style="color:var(--gold)">__SMALL_LATEST__</div><div class="s">0=最小票 1=最大票，全市场中位 0.50</div></div>
  <div class="metric"><div class="k">拥挤风险标记</div><div class="v" style="color:var(--gold)">__CROWD_FLAG__</div><div class="s">__CROWD_FLAG_NOTE__</div></div>
</div>

<div class="grid g2" style="margin-top:12px">
  <div class="card"><b>最同质信号 Top 5（正相关 = 同向拥挤；负相关 = 镜像拥挤）</b>
    <table id="tbl_crowd" style="margin-top:6px"></table>
    <div class="note">口径：__CROWD_WINDOW__ 起逐日截面相关均值（pearson，n≥1000/日）。__CROWD_NOTE__</div>
  </div>
  <div class="card"><b>Q5 组（买入组）小票暴露 · 年度演变</b>
    <div id="chart_small"></div>
    <div class="note">__SMALL_NOTE__</div>
  </div>
</div>

<h2>最新数据</h2>
<div class="card">
  <table id="tbl_latest"></table>
  <div class="note">PIT 口径：因子值基于当日及以前公开数据计算，无 as-of 回填、无前视。Top10 为因子值最高组（方向解释见分组分析）。</div>
</div>

<footer>
  口径附注：IC 主口径 = __HIC__ 日 rank IC（后复权 LEAD 收益），双类型（Pearson vs Rank）× h=1/5/20 对比见 IC 分析区口径表，审计快照 checks.ic 同 schema 存储；分组收益 = __HGRP__ 日远期收益逐日摊薄（= 调仓周期，非可交易组合，终审须组合层 A/B）；FDR q 为库级审计快照（BH + 自相关折减，h=20 口径），IC__HIC__ 本窗 t_adj=__IC5_TADJ__（lag1 ρ=__IC5_RHO__），阈值对自相关假设敏感；风格暴露 = 与风格代表因子的逐日截面 spearman 均值；拥挤度 = 库内同质信号扫描（近 60 日截面 pearson）+ Q5 组 size 分位中位数（完整拥挤度评估需公募持仓 / 两融等外部数据，此处为库内代理）；正交化增量 = 对生产因子逐日截面 OLS 残差 rank IC（h=__HGRP__）；多空腿以 A 股做空约束为前提解读。红绿灯仅初筛，入生产候选须组合层 A/B 终审。<br>
  数据源：QuantLab 因子湖 + DuckDB（只读）｜生成：__GENERATED__｜模板 dashboard-v3（scripts/factor_eval/build_dashboard.py，耗时 __ELAPSED__s）
</footer>
</div>

<script>
const DATA = __DATA__;

const C={q:'#9a9a9a',q1:'#5da9e0',q2:'#7bb8e8',q3:'#9a9a9a',q4:'#e0a35d',q5:'#e05d5d',ls:'#e6b455',ic:'#e05d5d',icir:'#55c4c4'};
function svgEl(w,h){const s=document.createElementNS('http://www.w3.org/2000/svg','svg');
  s.setAttribute('viewBox','0 0 '+w+' '+h);s.setAttribute('width','100%');return s}
function add(s,tag,attrs,txt){const e=document.createElementNS('http://www.w3.org/2000/svg',tag);
  for(const k in attrs)e.setAttribute(k,attrs[k]);if(txt!=null)e.textContent=txt;s.appendChild(e);return e}

/* ---- 折线图（双轴：右轴刻度 + 右轴线）---- */
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
    if(sec.length){
      const v2=ymin2+(ymax2-ymin2)*g/4;
      add(s,'text',{x:W-R+6,y:Y2(v2)+3,fill:'#55c4c4','font-size':9},v2.toFixed(2));}}
  if(sec.length){add(s,'line',{x1:W-R,y1:T,x2:W-R,y2:T+h,stroke:'#55c4c4','stroke-width':1,opacity:0.5});}
  const nx=Math.min(xs.length,8);
  for(let i=0;i<nx;i++){const xi=Math.round(i*(xs.length-1)/(nx-1));
    add(s,'text',{x:X(xi),y:H-10,'text-anchor':'middle',fill:'#9a9a9a','font-size':9},xlabels?xlabels[xi]:xs[xi]);}
  series.forEach(p=>{
    add(s,'polyline',{fill:'none',stroke:p.color,'stroke-width':2,
      points:p.data.map((v,i)=>X(i)+','+(p.axis2?Y2(v):Y(v))).join(' ')});
    if(p.data.length<=10)p.data.forEach((v,i)=>add(s,'circle',{cx:X(i),cy:p.axis2?Y2(v):Y(v),r:3,fill:p.color}));});
  let lx=L+4;series.forEach(p=>{add(s,'circle',{cx:lx,cy:T+2,r:3,fill:p.color});
    const t=add(s,'text',{x:lx+7,y:T+5,fill:'#bbb','font-size':10},p.name);lx+=7+t.getComputedTextLength()+16;});
}

/* ---- 多周期 IC 谱（主口径 h 高亮）---- */
(function(){
  const hs=DATA.decay.h;
  const el=document.getElementById('chart_decay');
  lineChart(el,hs,
    [{name:'IC',color:C.ic,data:DATA.decay.ic},
     {name:'ICIR（右轴）',color:C.icir,data:DATA.decay.icir,axis2:true}],
    hs.map(String),v=>v.toFixed(2));
  const svg=el.children[0];
  const W=520,L=46,R=52,T=16,h2=200;
  const w=W-L-R;
  const i=hs.indexOf(DATA.h_ic);
  const xh=i>=0?L+i/(hs.length-1)*w:L;
  add(svg,'line',{x1:xh,y1:T,x2:xh,y2:T+h2,stroke:'#e6b455','stroke-width':1.2,'stroke-dasharray':'4 3'});
  add(svg,'text',{x:xh+5,y:T+14,fill:'#e6b455','font-size':10,'font-weight':700},'主口径 h='+DATA.h_ic);
})();

/* ---- IC 直方图 ---- */
(function(){const el=document.getElementById('chart_hist');
  const W=520,H=230,L=46,R=12,T=14,B=30,w=W-L-R,h=H-T-B;
  const s=svgEl(W,H);el.appendChild(s);
  const arr=DATA.ic5_series,nb=30,mn=Math.min(...arr),mx=Math.max(...arr);
  const bins=new Array(nb).fill(0);
  arr.forEach(v=>{let i=Math.floor((v-mn)/(mx-mn)*nb);if(i>=nb)i=nb-1;bins[i]++;});
  const bm=Math.max(...bins);
  bins.forEach((b,i)=>{const bw=w/nb;
    add(s,'rect',{x:L+i*bw+0.5,y:T+h-b/bm*h,width:bw-1,height:b/bm*h,fill:'#e05d5d',opacity:0.85});});
  const mean=DATA.ic5.mean;
  const X=v=>L+(v-mn)/(mx-mn)*w;
  add(s,'line',{x1:X(mean),y1:T,x2:X(mean),y2:T+h,stroke:'#e6b455','stroke-width':1.5,'stroke-dasharray':'4 3'});
  add(s,'text',{x:X(mean)+4,y:T+10,fill:'#e6b455','font-size':10},'mean '+mean);
  for(let g=0;g<=4;g++){const v=mn+(mx-mn)*g/4;
    add(s,'line',{x1:L,y1:T+h,x2:W-R,y2:T+h,stroke:'#333'});
    add(s,'text',{x:L+(mx-mn)*g/4/(mx-mn)*w,y:T+h+14,'text-anchor':'middle',fill:'#9a9a9a','font-size':9},v.toFixed(2));}})();

/* ---- IC 自相关 ---- */
(function(){const el=document.getElementById('chart_acf');
  const W=520,H=230,L=46,R=12,T=14,B=30,w=W-L-R,h=H-T-B;
  const s=svgEl(W,H);el.appendChild(s);
  const acf=DATA.ic5_acf,bm=1;
  add(s,'line',{x1:L,y1:T+h/2,x2:W-R,y2:T+h/2,stroke:'#555'});
  acf.forEach((v,i)=>{const bw=w/acf.length,x=L+i*bw;
    const hh=Math.abs(v)/bm*h/2;
    add(s,'rect',{x:x+bw*0.25,y:v>=0?T+h/2-hh:T+h/2,width:bw*0.5,height:hh,
      fill:v>=0?'#55c4c4':'#e05d5d'});
    add(s,'text',{x:x+bw*0.5,y:H-10,'text-anchor':'middle',fill:'#9a9a9a','font-size':9},(i+1));});
  for(let g=0;g<=4;g++){const v=1-g/4;
    add(s,'text',{x:L-5,y:T+h*(1-v)/2+3,'text-anchor':'end',fill:'#9a9a9a','font-size':9},v.toFixed(1));
    add(s,'line',{x1:L,y1:T+h*(1-v)/2,x2:W-R,y2:T+h*(1-v)/2,stroke:'#333'});}})();

/* ---- 风格暴露水平条形图 ---- */
(function(){const el=document.getElementById('chart_style');
  const W=520,H=250,L=110,R=52,T=14,B=10;
  const entries=Object.entries(DATA.style_exposure);
  const gap=16,bh=(H-T-B-gap*(entries.length-1))/entries.length;
  const s=svgEl(W,H);el.appendChild(s);
  const mx=Math.max(...entries.map(e=>Math.abs(e[1].all)))*1.18||0.1;
  const w=W-L-R, X0=L+w/2;
  add(s,'line',{x1:X0,y1:T,x2:X0,y2:H-B,stroke:'#555'});
  entries.forEach((e,i)=>{
    const y=T+i*(bh+gap);
    const rho=e[1].all;
    const bw=Math.abs(rho)/mx*(w/2);
    const x=rho>=0?X0:X0-bw;
    const col=rho>=0?'#e05d5d':'#5dc98e';
    add(s,'rect',{x,y:y+2,width:Math.max(bw,1),height:bh-4,fill:col,opacity:0.85});
    const name=DATA.style_names[e[0]]||e[0];
    add(s,'text',{x:X0-8,y:y+bh/2+3,'text-anchor':'end',fill:'#ccc','font-size':11},name);
    add(s,'text',{x:(rho>=0?X0+bw+6:X0-bw-6),y:y+bh/2+3,'text-anchor':rho>=0?'start':'end',fill:col,'font-size':11,'font-weight':700},(rho>=0?'+':'')+rho.toFixed(3));
  });})();

/* ---- 风格年度演变表 ---- */
(function(){const t=document.getElementById('tbl_style');
  const years=Object.keys(DATA.style_exposure[Object.keys(DATA.style_exposure)[0]].yearly);
  let html='<tr><th>风格</th>';
  years.forEach(y=>html+='<th>'+y+'</th>');html+='<th>全期</th></tr>';
  Object.entries(DATA.style_exposure).forEach(([k,v])=>{
    html+='<tr><td>'+(DATA.style_names[k]||k)+'</td>';
    years.forEach(y=>{const r=v.yearly[y];
      if(r==null){html+='<td>—</td>';return;}
      html+='<td class="'+(r>=0?'pos':'neg')+'">'+(r>=0?'+':'')+r.toFixed(3)+'</td>';});
    html+='<td class="'+(v.all>=0?'pos':'neg')+'" style="font-weight:700">'+(v.all>=0?'+':'')+v.all.toFixed(3)+'</td></tr>';});
  t.innerHTML=html;})();

/* ---- 分组净值 ---- */
(function(){const keys=['Q1','Q2','Q3','Q4','Q5'];
  const series=keys.map(k=>({name:k,color:C[k.toLowerCase()],data:DATA.navs[k]}));
  series.push({name:'多空',color:C.ls,data:DATA.navs.LS});
  const xs=DATA.navs.Q1.map((_,i)=>i);
  lineChart(document.getElementById('chart_nav'),xs,series,DATA.nav_dates,v=>v.toFixed(1));})();

/* ---- 十分位（符号感知）---- */
(function(){const el=document.getElementById('chart_decile');
  const W=520,H=230,L=40,R=12,T=14,B=30,w=W-L-R,h=H-T-B;
  const s=svgEl(W,H);el.appendChild(s);
  const d=DATA.decile_bp,mx=Math.max(...d.map(Math.abs))||1;
  const Y0=T+h*0.9, Yv=v=>Y0-v/mx*h*0.8;
  add(s,'line',{x1:L,y1:Y0,x2:W-R,y2:Y0,stroke:'#333'});
  d.forEach((v,i)=>{const bw=w/10;
    const y=Math.min(Yv(v),Y0), hh=Math.abs(Yv(v)-Y0);
    add(s,'rect',{x:L+i*bw+1,y,width:bw-2,height:Math.max(hh,0.5),
      fill:i===9?'#e6b455':(i===0?'#e0a35d':'#5da9e0'),opacity:i===9?1:0.8});
    add(s,'text',{x:L+i*bw+bw/2,y:v>=0?y-4:y+12,'text-anchor':'middle',fill:'#bbb','font-size':9},v);
    add(s,'text',{x:L+i*bw+bw/2,y:H-12,'text-anchor':'middle',fill:'#9a9a9a','font-size':9},'D'+(i+1));});})();

/* ---- 正交化增量对比（符号感知）---- */
(function(){const el=document.getElementById('chart_resid');
  const W=520,H=190,L=40,R=12,T=20,B=30,w=W-L-R,h=H-T-B;
  const s=svgEl(W,H);el.appendChild(s);
  const vals=[DATA.resid?DATA.resid.raw:0, DATA.resid?DATA.resid.resid:0];
  const mx=Math.max(...vals.map(Math.abs))*1.2||0.01;
  const Y0=T+h*0.55, Yv=v=>Y0-v/mx*(h*0.45);
  add(s,'line',{x1:L,y1:Y0,x2:W-R,y2:Y0,stroke:'#333'});
  const items=[['原始 IC',vals[0],C.q5],['正交化残差 IC',vals[1],'#e6b455']];
  items.forEach((it,i)=>{const bw=w/items.length*0.5,x=L+i*(w/items.length)+bw*0.25;
    const y=Math.min(Yv(it[1]),Y0), hh=Math.abs(Yv(it[1])-Y0);
    add(s,'rect',{x,y,width:bw,height:Math.max(hh,0.5),fill:it[2]});
    add(s,'text',{x:x+bw/2,y:it[1]>=0?y-6:y+16,'text-anchor':'middle',fill:'#eee','font-size':12,'font-weight':700},(it[1]>=0?'+':'')+it[1].toFixed(4));
    add(s,'text',{x:x+bw/2,y:T+h+14,'text-anchor':'middle',fill:'#9a9a9a','font-size':10},it[0]);});})();

/* ---- Q5 小票暴露年度条形 ---- */
(function(){const el=document.getElementById('chart_small');
  const rows=DATA.small_cap_exposure.yearly;
  const W=520,H=230,L=46,R=12,T=18,B=30,w=W-L-R,h=H-T-B;
  const s=svgEl(W,H);el.appendChild(s);
  const Y=v=>T+h-v/1.0*h;
  for(let g=0;g<=4;g++){const v=g/4;
    add(s,'line',{x1:L,y1:Y(v),x2:W-R,y2:Y(v),stroke:'#333'});
    add(s,'text',{x:L-5,y:Y(v)+3,'text-anchor':'end',fill:'#9a9a9a','font-size':9},v.toFixed(2));}
  add(s,'line',{x1:L,y1:Y(0.5),x2:W-R,y2:Y(0.5),stroke:'#555','stroke-dasharray':'4 3'});
  add(s,'text',{x:W-R-4,y:Y(0.5)-4,'text-anchor':'end',fill:'#888','font-size':9},'市场中位 0.50');
  const bw=w/rows.length*0.36;
  rows.forEach((r,i)=>{
    const xc=L+(i+0.5)*(w/rows.length);
    const v=r.q5_size_pct;
    add(s,'rect',{x:xc-bw/2,y:Y(v),width:bw,height:Y(0)-Y(v),fill:'#e6b455',opacity:0.9});
    add(s,'text',{x:xc,y:Y(v)-5,'text-anchor':'middle',fill:'#e6b455','font-size':11,'font-weight':700},v.toFixed(3));
    add(s,'text',{x:xc,y:H-12,'text-anchor':'middle',fill:'#9a9a9a','font-size':10},r.year);});})();

/* ---- 拥挤度 top 表 ---- */
(function(){const t=document.getElementById('tbl_crowd');
  const cr=DATA.crowding;
  let html='<tr><th>因子</th><th>ρ（60日均值）</th><th>方向</th></tr>';
  cr.top_positive.forEach(r=>{html+='<tr><td>'+r.factor+'</td><td class="pos">+'+r.rho.toFixed(3)+'</td><td>同向拥挤</td></tr>';});
  cr.top_negative.forEach(r=>{html+='<tr><td>'+r.factor+'</td><td class="neg">'+r.rho.toFixed(3)+'</td><td>镜像信号</td></tr>';});
  t.innerHTML=html;})();

/* ---- 分组表（最优组标注数据驱动）---- */
(function(){const t=document.getElementById('tbl_groups');
  const rows=[1,2,3,4,5].map(i=>['Q'+i,'分组'+i+(i===DATA.best_q?'（最优）':'')]);
  rows.push(['LS','多空组合（Q5−Q1）']);
  let html='<tr><th>分组</th><th>'+DATA.h_grp+'日均值(bp)</th><th>年化收益</th><th>年化波动</th><th>夏普</th><th>最大回撤</th><th>月度胜率</th></tr>';
  rows.forEach(r=>{const g=DATA.groups[r[0]];
    html+='<tr><td>'+r[1]+'</td><td>'+(g.mean_fwd_bp>0?'+':'')+g.mean_fwd_bp+'</td>'+
      '<td class="'+(g.ann_ret>=0?'pos':'neg')+'">'+(g.ann_ret*100).toFixed(1)+'%</td>'+
      '<td>'+(g.ann_vol*100).toFixed(1)+'%</td><td>'+g.sharpe+'</td>'+
      '<td class="neg">'+(g.mdd*100).toFixed(1)+'%</td><td>'+(g.mon_win*100).toFixed(0)+'%</td></tr>';});
  t.innerHTML=html;})();

(function(){const t=document.getElementById('tbl_latest');
  let html='<tr><th>日期</th><th>股票代码</th><th>因子值</th></tr>';
  DATA.latest.top.forEach(r=>{html+='<tr><td>'+DATA.latest.date+'</td><td>'+r.code+'</td><td>'+r.value+'</td></tr>';});
  t.innerHTML=html;})();

/* ---- 月度 IC 热力图 ---- */
(function(){const t=document.getElementById('tbl_monthly');
  const m=DATA.monthly_ic5;
  let html='<tr><th>年</th>';m.months.forEach(x=>html+='<th>'+x+'月</th>');html+='</tr>';
  m.years.forEach((y,i)=>{html+='<tr><td>'+y+'</td>';
    m.values[i].forEach(v=>{let bg='',c='#777';
      if(v!=null){bg='rgba(224,93,93,'+Math.min(Math.abs(v)*1.8,0.85)+')';c=v>0.15?'#fff':'#bbb';
        if(v<0)bg='rgba(93,201,142,'+Math.min(Math.abs(v)*1.8,0.85)+')';}
      html+='<td style="background:'+bg+';color:'+c+'">'+(v==null?'—':(v>0?'+':'')+v.toFixed(3))+'</td>';});
    html+='</tr>';});
  t.innerHTML=html;})();
</script>
</body>
</html>"""

    I = D["ic5"]
    if X["light"] == "🔴":
        head = "红色：" + X["verdict_summary"]
    elif X["light"] == "🟡":
        head = "黄色：" + X["verdict_summary"]
    else:
        head = "绿色：全部检验通过"
    pit_cls = ("var(--green)" if X["verdict"] == "PASS"
               else ("var(--red)" if X["verdict"] == "FAIL" else "var(--gold)"))
    R = {
        "__NAME__": X["name"], "__HIC__": str(D["h_ic"]),
        "__HGRP__": str(D["h_grp"]),
        "__DESC__": X["desc"], "__START__": X["start"], "__END__": X["end"],
        "__LATEST__": X["latest_date"], "__CATEGORY__": X["category"],
        "__TAGS__": tags_html, "__RATIONALE__": X["rationale"],
        "__NPOOL__": str(X["n_pool"]), "__NAUDITED__": str(X["n_audited"]),
        "__IC5_MEAN__": f"{I['mean']:+.4f}",
        "__IC5_MEAN_CLS__": "pos" if I["mean"] > 0 else "neg",
        "__IC5_ICIR__": f"{I['icir']:+.3f}", "__IC5_STD__": f"{I['std']:.4f}",
        "__IC5_PEAR__": f"{I['pearson_mean']:+.4f}",
        "__IC5_TADJ__": f"{I['t_adj']:.2f}", "__IC5_NEFF__": f"{I['n_eff']:.0f}",
        "__IC5_RHO__": f"{I['lag1_autocorr']:.3f}", "__IC5_N__": str(I["n_days"]),
        "__IC5_WIN__": f"{I['win_rate']*100:.0f}%",
        "__IC5_PPOS__": f"{I['p_ic_pos']*100:.1f}%",
        "__IC5_PNEG__": f"{I['p_ic_neg']*100:.1f}%",
        "__IC5_SKEW__": f"{I['skew']:.3f}", "__IC5_KURT__": f"{I['kurt']:.3f}",
        "__IC5_P__": (f"{math.erfc(abs(I['t_adj']) / math.sqrt(2)):.3f}"
                      if I["t_adj"] is not None else "—"),
        "__FDR_Q__": str(X["q"]) if X["q"] is not None else "—",
        "__T_LIB__": (f"｜库级 h=20 口径 t={X['t_lib']}"
                      if X["t_lib"] is not None else ""),
        "__HALF__": X["half_life"],
        "__LIGHT__": X["light"], "__VERDICT_HEAD__": head,
        "__VERDICT_SUMMARY__": X["verdict_summary"],
        "__VERDICT_TABLE__": X["verdict_table"],
        "__DECAY_NOTE__": X["decay_note"], "__HIST_NOTE__": X["hist_note"],
        "__ICMX_ROWS__": X["icmx_rows"], "__ICMX_NOTE__": X["icmx_note"],
        "__ACF_NOTE__": X["acf_note"], "__MONTHLY_NOTE__": X["monthly_note"],
        "__STYLE_NOTE__": X["style_note"],
        "__STYLE_DRIFT_NOTE__": X["style_drift_note"],
        "__DECILE_NOTE__": X["decile_note"], "__LS_TABLE__": X["ls_table"],
        "__LS_NOTE__": X["ls_note"], "__YEARLY_TABLE__": X["yearly_table"],
        "__YEARLY_NOTE__": X["yearly_note"], "__RESID_NOTE__": X["resid_note"],
        "__CORR_ROWS__": X["corr_rows"], "__CORR_NOTE__": X["corr_note"],
        "__CROWD_N07__": str(D["crowding"]["n_rho_gt_07"]),
        "__CROWD_N05__": str(D["crowding"]["n_rho_gt_05"]),
        "__CROWD_SCANNED__": str(D["crowding"]["n_factors_scanned"]),
        "__CROWD_PCT05__": f"{D['crowding']['n_rho_gt_05'] / max(D['crowding']['n_factors_scanned'], 1) * 100:.0f}%",
        "__CROWD_WINDOW__": D["crowding"]["window"],
        "__CROWD_NOTE__": X["crowd_note"],
        "__SMALL_LATEST__": f"{D['small_cap_exposure']['yearly'][-1]['q5_size_pct']:.2f}",
        "__SMALL_NOTE__": X["small_note"],
        "__CROWD_FLAG__": X["crowd_flag"],
        "__CROWD_FLAG_NOTE__": X["crowd_flag_note"],
        "__GENERATED__": f"{datetime.now():%Y-%m-%d %H:%M}",
        "__ELAPSED__": str(X["elapsed"]),
        "__DATA__": _json.dumps(D, ensure_ascii=False),
    }
    html = TEMPLATE
    for k, v in R.items():
        html = html.replace(k, str(v))
    # PIT 颜色按 verdict 内联（红/黄/绿）
    html = html.replace(
        '<div class="v">__PIT_CLS__</div>',
        f'<div class="v" style="color:{pit_cls}">{X["verdict"]}</div>')
    return html


if __name__ == "__main__":
    main()
