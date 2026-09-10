#!/usr/bin/env python3
"""Phase 2/Q3：因子选股能否让"尾盘买→次开卖"转正？
=================================================================
目标：隔夜段无条件为负（−4bp/日），检验因子条件化能否把 topN 组合
      拉到 15bp 成本线之上（G2 门槛 +25bp 毛）。

口径 A（长样本，日线）：T 日收盘买入 → T+1 开盘卖出
    target_T = adj_open_{T+1} / close_T - 1        （除权调整挂 T+1）
口径 B（2025+，分钟）：T 日 14:56 买入 → T+1 开盘卖出（pseudo_daily.exec_ret）

方法：
  1. 单因子筛查：全样本 RankIC + top100 组合日均（逐日横截面等权 → 时序 t）
  2. IS/OOS 合成：IS = 2022-2024 定方向与入选（|IC| 排序），OOS = 2025-2026 评估
  3. 同一合成在口径 B 上复验（真实 14:56 执行）

输出：reports/overnight/q3_factor.json
"""
from __future__ import annotations

import glob
import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
FSDB_GLOB = str(ROOT / "data/lake/factor/overnight/daily_fsdb/*.parquet").replace("\\", "/")
PD_GLOB = str(ROOT / "data/lake/factor/overnight/pseudo_daily/*.parquet").replace("\\", "/")
DIV = str(ROOT / "data/lake/clean/mirror/dividend_events.parquet").replace("\\", "/")
FACTOR_DIR = ROOT / "data/lake/factor"
OUT = ROOT / "reports/overnight"

TOPN = 100
COST_BP = 15.0
TRADING_DAYS = 244.0

# 精选因子池：覆盖量价/流动性/风险/财务/事件族，排除已知冗余与宽表类
FACTORS = [
    "size", "amihud_20", "turnover", "avg_amount_20", "amount_std_20",
    "volatility_20", "downside_volatility_20", "skewness_20", "max_return_20",
    "reversal_5", "reversal_10", "momentum_20", "momentum_60", "momentum_120",
    "overnight_mom_20", "overnight", "rsi_14", "amplitude_20", "price_position_250",
    "chip_vwap_bias_250", "turnover_std_20", "total_mcap", "bp", "ep",
    "sue_i", "roe", "climb_peak_20", "moderate_risk_20", "ev_high_vol_20",
    "lockup_pressure_60", "tide_all_20", "retail_buy_20", "inst_buy_20",
]
IS_END = "2024-12-31"          # IS: <= 该日；OOS: 之后


def build_target(con) -> None:
    con.execute(f"""
    CREATE TEMP TABLE ev1 AS
    SELECT code, date, SUM(COALESCE(fenhong,0)) fh, SUM(COALESCE(songzhuangu,0)) sz,
           SUM(COALESCE(peigu,0)) pg,
           CASE WHEN SUM(COALESCE(peigu,0)) > 0
                THEN SUM(COALESCE(peigu,0)*COALESCE(peigujia,0))/SUM(COALESCE(peigu,0)) ELSE 0 END pj
    FROM read_parquet('{DIV}') GROUP BY code, date
    """)
    con.execute(f"""
    CREATE TEMP TABLE px AS
    SELECT *, min(date) OVER (PARTITION BY code) AS first_dt
    FROM read_parquet('{FSDB_GLOB}')
    """)
    con.execute("""
    CREATE TEMP TABLE tgt AS
    WITH x AS (
      SELECT code, date, close, vol, amount, is_st, first_dt,
             LEAD(date) OVER w AS nd, LEAD(open) OVER w AS nopen
      FROM px WINDOW w AS (PARTITION BY code ORDER BY date)
    )
    SELECT x.code, x.date, x.close AS close_t, x.amount, x.vol, x.nd,
           CASE WHEN e.code IS NULL THEN x.nopen
                ELSE x.nopen*(1+COALESCE(e.sz,0)+COALESCE(e.pg,0))
                     + COALESCE(e.fh,0) - COALESCE(e.pg,0)*COALESCE(e.pj,0)
           END AS adj_nopen,
           (COALESCE(x.is_st,FALSE) = FALSE
            AND substr(x.code,1,1) NOT IN ('4','8') AND substr(x.code,1,3) <> '920'
            AND (x.first_dt <= DATE '2021-08-01'
                 OR x.date >= x.first_dt + INTERVAL 120 DAY)) AS in_ex
    FROM x LEFT JOIN ev1 e ON e.code = x.code AND e.date = x.nd
    """)
    con.execute("""
    CREATE TEMP TABLE tgt2 AS
    SELECT code, date, close_t, amount, adj_nopen/close_t - 1 AS target
    FROM tgt
    WHERE in_ex AND close_t > 0 AND adj_nopen IS NOT NULL AND vol > 0
    """)


def factor_stats(con, name: str) -> pd.DataFrame:
    fs = sorted(glob.glob(str(FACTOR_DIR / name / "*.parquet")))
    if not fs:
        return pd.DataFrame()
    path = fs[0].replace("\\", "/")
    # 兼容多 part：目录内所有文件
    glob_p = str(FACTOR_DIR / name / "*.parquet").replace("\\", "/")
    return con.execute(f"""
    WITH f AS (
      SELECT code, date, value FROM read_parquet('{glob_p}')
      WHERE value IS NOT NULL AND date >= DATE '2022-01-01'
    ), j AS (
      SELECT t.date, t.code, t.target, f.value
      FROM tgt2 t JOIN f ON f.code = t.code AND f.date = t.date
    ), r AS (
      SELECT date, target, value,
             rank() OVER (PARTITION BY date ORDER BY value)  AS rv,
             rank() OVER (PARTITION BY date ORDER BY target) AS rt,
             count(*) OVER (PARTITION BY date)               AS n
      FROM j
    )
    SELECT date,
           corr(rv, rt) AS ic,
           avg(CASE WHEN rv >= n - {TOPN} + 1 THEN target END) AS top_long,
           avg(CASE WHEN rv <= {TOPN}        THEN target END) AS bottom_long,
           avg(target) AS all_long,
           count(*) AS n
    FROM r GROUP BY date ORDER BY date
    """).df()


def series_stats(s: pd.Series) -> dict:
    s = s.dropna()
    if len(s) < 5:
        return {}
    mu, sd, n = s.mean(), s.std(ddof=1), len(s)
    return {
        "days": int(n),
        "mean_bp": float(mu * 1e4),
        "t": float(mu / (sd / np.sqrt(n))) if sd > 0 else None,
        "win": float((s > 0).mean()),
        "ann_pct": float(mu * TRADING_DAYS * 100),
        "ann_net_pct": float((mu - COST_BP / 1e4) * TRADING_DAYS * 100),
    }


def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("PRAGMA threads=6")
    build_target(con)
    n = con.execute("SELECT count(*) FROM tgt2").fetchone()[0]
    print(f"[target] rows={n:,}（T 收盘买 → T+1 开盘卖，除权已调整）")

    # ── 1. 单因子筛查 ────────────────────────────────────────────
    rows, daily = {}, {}
    res: dict = {}
    for name in FACTORS:
        d = factor_stats(con, name)
        if d.empty:
            print(f"  {name:>22s}  (no data)")
            continue
        ic = d.ic.dropna()
        st_top = series_stats(d.top_long)
        st_bot = series_stats(d.bottom_long)
        st_all = series_stats(d.all_long)
        rows[name] = {
            "ic_mean": float(ic.mean()),
            "icir": float(ic.mean() / ic.std(ddof=1)) if ic.std(ddof=1) > 0 else None,
            "ic_pos_share": float((ic > 0).mean()),
            "top100": st_top,
            "bottom100": st_bot,
            "all": st_all,
            "spread_bp": (st_top["mean_bp"] - st_bot["mean_bp"])
                          if st_top and st_bot else None,
        }
        daily[name] = d.set_index("date")[["top_long", "bottom_long", "all_long", "ic"]]
        print(f"  {name:>22s} IC={ic.mean():+.4f} top100={st_top['mean_bp']:+7.2f}bp "
              f"(t={st_top['t']:+5.2f}) spread={rows[name]['spread_bp']:+7.2f}bp")
    print(f"[screen] {len(rows)} 因子，耗时 {time.time()-t0:.0f}s")

    res["single"] = rows

    # ── 2. IS/OOS 合成（IS 双向择优：top/bottom 取更优方向，且必须 IS 为正才入选）──
    IS_TS = pd.Timestamp(IS_END)
    cand = []
    for k, d in daily.items():
        is_ = d[d.index <= IS_TS]
        a = series_stats(is_["top_long"].dropna())
        b = series_stats(is_["bottom_long"].dropna())
        if not a or not b:
            continue
        dirv, val, tag = (1.0, a, "top") if a["mean_bp"] >= b["mean_bp"] else (-1.0, b, "bottom")
        oos = series_stats(d[d.index > IS_TS]["top_long" if dirv > 0 else "bottom_long"].dropna())
        cand.append({"factor": k, "dir": dirv, "tag": tag,
                     "is_bp": val["mean_bp"], "is_t": val["t"],
                     "oos_bp": oos.get("mean_bp")})
    cand.sort(key=lambda x: -x["is_bp"])
    pos = [c for c in cand if c["is_bp"] > 0]
    top_k = [(c["factor"], None, c["dir"]) for c in pos[:8]]
    print("\n[IS 2022-2024 双向择优（IS 为正才入选）]")
    for c in cand[:12]:
        mark = "✓" if c["is_bp"] > 0 else "✗"
        print(f"  {mark} {c['factor']:>22s} dir={c['dir']:+.0f}({c['tag']:>6s}) "
              f"IS={c['is_bp']:+7.2f}bp (t={c['is_t']:+5.2f}) → OOS={c['oos_bp']:+7.2f}bp")
    print(f"  → 入选 {len(top_k)} 个（IS 为正）")

    dir_map = {k: float(s) for k, a, s in top_k}
    sel = list(dir_map.keys())
    res["selected"] = [{"factor": c["factor"], "dir": c["dir"], "tag": c["tag"],
                        "is_bp": c["is_bp"], "is_t": c["is_t"],
                        "oos_bp": c["oos_bp"]} for c in pos[:8]]

    # 逐因子 rank 宽表（逐个读入 pandas 再合成，避免巨型 SQL）
    base = con.execute("SELECT code, date, target FROM tgt2").df()
    print(f"[composite] base rows={len(base):,}，拼接 {len(sel)} 因子 rank …")
    for i, k in enumerate(sel):
        glob_p = str(FACTOR_DIR / k / "*.parquet").replace("\\", "/")
        fv = con.execute(f"""
            SELECT code, date, value FROM read_parquet('{glob_p}')
            WHERE value IS NOT NULL AND date >= DATE '2022-01-01'
        """).df()
        fv[f"r{i}"] = fv.groupby("date")["value"].rank()
        base = base.merge(fv[["code", "date", f"r{i}"]], on=["code", "date"], how="left")
        del fv
    rank_cols = [f"r{i}" for i in range(len(sel))]
    base["n_ok"] = base[rank_cols].notna().sum(axis=1)
    base = base[base.n_ok >= max(3, len(sel) // 2)].copy()
    base["score"] = sum(dir_map[k] * base[f"r{i}"] for i, k in enumerate(sel)) / base.n_ok
    print(f"[composite] rows={len(base):,}")

    comp_daily = []
    for date, g in base.groupby("date"):
        g = g.dropna(subset=["score", "target"])
        if len(g) < 200:
            continue
        top = g.nlargest(TOPN, "score")
        comp_daily.append({"date": date, "top": top.target.mean(),
                           "all": g.target.mean(), "n": len(g)})
    cd = pd.DataFrame(comp_daily).set_index("date").sort_index()
    res["composite"] = {
        "factors": sel,
        "all_oos": series_stats(cd[cd.index > pd.Timestamp(IS_END)]["top"]),
        "all_is": series_stats(cd[cd.index <= pd.Timestamp(IS_END)]["top"]),
        "full": series_stats(cd["top"]),
        "universe_mean": series_stats(cd["all"]),
        "by_year": {str(y): series_stats(g["top"]) for y, g in cd.groupby(cd.index.year)},
    }
    c = res["composite"]
    print(f"\n[composite] days={len(cd)} rows={len(base):,}")
    if not c.get("full"):
        raise SystemExit("[abort] 合成组合无有效交易日")
    print("\n[合成 top100]  全期 {mean_bp:+.2f}bp t={t:.2f} | IS {is_bp:+.2f}bp | OOS {oos_bp:+.2f}bp".format(
        mean_bp=c["full"]["mean_bp"], t=c["full"]["t"] or 0,
        is_bp=c["all_is"]["mean_bp"], oos_bp=c["all_oos"]["mean_bp"]))
    print(f"  全期扣费年化 {c['full']['ann_net_pct']:+.1f}% | 全域等权基准 {c['universe_mean']['mean_bp']:+.2f}bp")
    for y, st in c["by_year"].items():
        print(f"  {y}: {st['mean_bp']:+7.2f}bp  t={st['t']:+5.2f}  净年化={st['ann_net_pct']:+7.1f}%")

    # ── 3. 口径 B：2025+ 真实 14:56 执行复验 ─────────────────────
    try:
        pdm = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob(PD_GLOB))],
                        ignore_index=True)
        pdm = pdm[["code", "date", "exec_ret"]]
        b = base.merge(pdm, on=["code", "date"], how="inner")
        b = b.dropna(subset=["score", "exec_ret"])
        rows_b = []
        for date, g in b.groupby("date"):
            if len(g) < 200:
                continue
            rows_b.append(g.nlargest(TOPN, "score").exec_ret.mean())
        res["composite_exec_1456"] = series_stats(pd.Series(rows_b))
        st = res["composite_exec_1456"]
        print(f"\n[口径B 14:56 执行] {st.get('mean_bp', float('nan')):+.2f}bp "
              f"t={st.get('t', float('nan')):+.2f} 净年化={st.get('ann_net_pct', float('nan')):+.1f}%")
    except Exception as e:                                     # noqa: BLE001
        res["composite_exec_1456"] = {"error": str(e)[:200]}

    # ── 4. 容量约束：流动性下限分位 → 组合收益（alpha 是否只存在于买不到的票）──
    amt = con.execute("SELECT code, date, amount FROM tgt2").df()
    base = base.merge(amt, on=["code", "date"], how="left")
    print("\n[容量约束] 逐日在成交额 ≥ q 分位的池内选 score top100")
    print(f"{'q':>6} {'毛bp':>8} {'t':>6} {'OOS bp':>8} {'top100中位成交额(万)':>20} {'扣15bp年化':>10} {'扣30bp年化':>10}")
    cap = {}
    for q in [0.0, 0.3, 0.5, 0.7]:
        rows_c = []
        for date, g in base.groupby("date"):
            g = g.dropna(subset=["score", "target", "amount"])
            if len(g) < 200:
                continue
            thr = g.amount.quantile(q)
            gg = g[g.amount >= thr]
            if len(gg) < TOPN:
                continue
            top = gg.nlargest(TOPN, "score")
            rows_c.append({"date": date, "top": top.target.mean(),
                           "med_amt": top.amount.median()})
        cc = pd.DataFrame(rows_c).set_index("date")
        st = series_stats(cc["top"])
        st_oos = series_stats(cc[cc.index > IS_TS]["top"])
        cap[f"q{int(q*100)}"] = {
            **st, "oos_bp": st_oos.get("mean_bp"),
            "median_amount_top100": float(cc.med_amt.median()),
            "net_ann_15bp": st["ann_net_pct"],
            "net_ann_30bp": float((st["mean_bp"] - 30) / 1e4 * TRADING_DAYS * 100),
        }
        print(f"{q:>6.0%} {st['mean_bp']:>8.2f} {st['t']:>6.2f} "
              f"{st_oos.get('mean_bp', float('nan')):>8.2f} "
              f"{cc.med_amt.median()/1e4:>20,.0f} "
              f"{st['ann_net_pct']:>9.1f}% "
              f"{(st['mean_bp']-30)/1e4*TRADING_DAYS*100:>9.1f}%")
    res["capacity"] = cap

    # 存合成打分（供持有期扫描等后续脚本复用）
    sc_dir = ROOT / "data/lake/factor/overnight/composite_score"
    sc_dir.mkdir(parents=True, exist_ok=True)
    base[["code", "date", "score", "n_ok", "amount"]].to_parquet(
        sc_dir / "score.parquet", index=False)
    (sc_dir / "meta.json").write_text(json.dumps(
        {"factors": sel, "dir": dir_map, "is_end": IS_END,
         "source": res.get("selected", [])}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"[save] 合成打分 → {sc_dir}/score.parquet ({len(base):,} 行)")

    (OUT / "q3_factor.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
    print(f"\n[done] {time.time()-t0:.0f}s → reports/overnight/q3_factor.json")


if __name__ == "__main__":
    main()
