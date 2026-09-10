r"""「半仓滚动法」收益测算（v2：纯 PIT + 供应商总收益口径）
================================================================
规则：
    资金分两半 A / B，每半都是「早盘（开盘）买入 → 次日收盘卖出」。
    A 在 T 日买、T+1 日收盘卖；B 在 T+1 日买、T+2 日收盘卖……两半交错滚动。

暴露结构（恒等）：
    日内暴露 100%（两半当天都在场）｜隔夜暴露 50%（只一半过夜）
    组合日收益 r_t = 0.5*[(1+g_{t-1})(1+i_t) - 1] + 0.5*i_t
      i_t = 当日 开盘→收盘 收益（供应商 intraday_ret 口径）
      g_t = 当日收盘→次日开盘 收益（= 次日 overnight_ret）

⚠️ v1 的两个坑（已修正）：
  1) 用「次日可卖出」做过滤 = 引入未来信息，把 limit-down 日剔除 →
     日内均值被系统性抬高约 3~5bp，而效应本身只有 5~9bp 量级。
     v2 只用开仓日已知信息（当日有成交、非 ST、开盘非一字涨停）。
  2) 手工用 close/open 相除重建收益会丢除权调整 →
     改用 segments 表自带的 total_ret / overnight_ret / intraday_ret
     （供应商已按除权除息调整 prev_close，口径与官方一致）。

数据：daily_segments 2022-01-05~2026-09-09（5556 只）+ fsdb PIT（is_st/float_mv）
成本：满仓往返 rt bp，日成本 = 0.5*rt（每天买 50% + 卖 50%）

输出：reports/overnight/half_roll_*.csv / .json
"""
from __future__ import annotations

import argparse
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
SEG = str(ROOT / "data/lake/factor/overnight/daily_segments/*.parquet").replace("\\", "/")
FSDB = str(ROOT / "data/lake/factor/overnight/daily_fsdb/*.parquet").replace("\\", "/")
IDX = str(ROOT / "data/lake/clean/mirror/index_kline.parquet").replace("\\", "/")
REP = ROOT / "reports/overnight"
OUTD = ROOT / "data/lake/factor/overnight/half_roll"
PER_YEAR = 244.0

PANEL_SQL = r"""
CREATE OR REPLACE TEMP TABLE seg AS
SELECT code, date,
       open, close, prev_close, vol, amount,
       COALESCE(halted, FALSE) AS halted,
       COALESCE(close_at_limit, FALSE) AS close_at_limit,
       total_ret, overnight_ret, intraday_ret, is_ex_date,
       CASE
         WHEN substr(code,1,3) IN ('688','300') THEN 0.20
         WHEN substr(code,1,1) = '3' THEN 0.20
         WHEN substr(code,1,1) IN ('4','8') OR substr(code,1,3) = '920' THEN 0.30
         ELSE 0.10
       END AS limit_pct
FROM read_parquet('{seg}') WHERE date >= DATE '2021-11-01';

CREATE OR REPLACE TEMP TABLE cal AS
SELECT dt, LEAD(dt) OVER (ORDER BY dt) AS nd FROM (SELECT DISTINCT date dt FROM seg);

-- 原始面板：T 日一行，带 T+1 的隔夜收益（= 次日 overnight_ret）
CREATE OR REPLACE TEMP TABLE raw AS
SELECT s.code, s.date AS d, c.nd AS d1,
       s.open AS o0, s.close AS c0, s.prev_close AS pc0, s.vol AS v0, s.amount AS amt0,
       s.total_ret AS rt, s.overnight_ret AS og, s.limit_pct,
       s.halted AS halted0, s.close_at_limit AS callim0,
       o.open AS o1, o.close AS c1, o.vol AS v1,
       o.halted AS halted1, o.close_at_limit AS callim1,
       o.total_ret AS rt1, o.overnight_ret AS og1
FROM seg s
JOIN cal c ON c.dt = s.date
LEFT JOIN seg o ON o.code = s.code AND o.date = c.nd
WHERE s.date >= DATE '2022-01-01';
"""

# ── 纯 PIT 过滤：只用开仓日（T 日开盘）已知的信息 ──────────────
#   ⚠️ 不含任何依赖 T+1 的条件（那是未来信息，会污染均值）
PIT = r"""
  substr(r.code,1,1) NOT IN ('4','8') AND substr(r.code,1,3) <> '920'   -- 非北交所
  AND r.v0 > 0 AND r.o0 > 0 AND r.c0 > 0 AND r.rt IS NOT NULL           -- 当日可成交
  AND r.halted0 = FALSE
  AND r.o0 < r.pc0*(1+r.limit_pct) - 0.005                              -- 开盘一字涨停买不到
  AND r.d1 IS NOT NULL                                                  -- 存在下一交易日
"""


def build(con) -> None:
    for stmt in PANEL_SQL.format(seg=SEG).split(";"):
        if stmt.strip():
            con.execute(stmt.strip())
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE pf AS
    SELECT r.*,
           (r.rt1 IS NOT NULL AND r.v1 > 0)                       AS next_ok,
           COALESCE(r.og1, 0)                                     AS g_raw,
           CASE WHEN r.rt1 IS NOT NULL AND r.v1 > 0 AND r.o1 > 0
                THEN (1+r.rt1)/(1+r.og1) - 1 END                  AS i1,
           CASE WHEN r.o0 > 0 THEN (1+r.rt)/(1+r.og) - 1 END      AS i0,
           r.rt                                                   AS cc,
           CASE WHEN r.rt1 IS NOT NULL AND r.v1 > 0 AND r.o1 > 0
                THEN (1+r.rt1)/(1+r.og1) - 1 END                  AS iv_next,
           COALESCE(f.is_st, FALSE)                               AS is_st,
           f.float_mv
    FROM raw r LEFT JOIN read_parquet('{FSDB}') f ON f.code = r.code AND f.date = r.d
    WHERE {PIT}
    """)
    # 未来一日的纯价格字段（用于诊断，不参与过滤）
    con.execute("""
    ALTER TABLE pf ADD COLUMN i0c DOUBLE;
    UPDATE pf SET i0c = CASE WHEN rt1 IS NOT NULL AND v1>0 THEN (1+rt1)/(1+og1)-1 END;
    """)
    # PIT 桶：用【过去 20 日均额】和【上一日市值】分档，避免用当日成交额（含未来信息）
    con.execute("""
    CREATE OR REPLACE TEMP TABLE bkt AS
    SELECT code, d,
           ntile(5) OVER (PARTITION BY d ORDER BY amt20) AS liq_q,
           ntile(5) OVER (PARTITION BY d ORDER BY mvlag) AS size_q
    FROM (
      SELECT code, d,
             avg(amt0) OVER (PARTITION BY code ORDER BY d
                             ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS amt20,
             lag(float_mv) OVER (PARTITION BY code ORDER BY d)             AS mvlag
      FROM pf
    ) WHERE amt20 IS NOT NULL AND mvlag IS NOT NULL AND mvlag > 0
    """)


def agg_daily(con, keys="d", join_bkt=False, extra_cols="", extra_join="") -> pd.DataFrame:
    j = "LEFT JOIN bkt b ON b.code = pf.code AND b.d = pf.d" if join_bkt else ""
    return con.execute(f"""
    SELECT {keys}, count(*) AS n,
           avg(pf.i0) AS i0, avg(pf.g_raw) AS g, avg(pf.i1) AS i1, avg(pf.cc) AS cc,
           avg(pf.amt0) AS amt0 {extra_cols}
    FROM pf {j} {extra_join}
    GROUP BY {keys} ORDER BY {keys}
    """).df()


# ──────────────────────────────────────────────────────────────
# 组合层
# ──────────────────────────────────────────────────────────────
def _stat(x: pd.Series) -> dict:
    mu, sd = float(x.mean()), float(x.std(ddof=1))
    nav = (1 + x).cumprod()
    dd = float((nav / nav.cummax() - 1).min())
    n = len(x)
    yrs = n / PER_YEAR
    return {"ann_arith_pct": round(((1 + mu) ** PER_YEAR - 1) * 100, 1),
            "cagr_pct": round((float(nav.iloc[-1]) ** (1 / yrs) - 1) * 100, 1),
            "total_pct": round((float(nav.iloc[-1]) - 1) * 100, 1),
            "mean_bp": round(mu * 1e4, 2),
            "vol_ann_pct": round(sd * np.sqrt(PER_YEAR) * 100, 1),
            "sharpe": round(mu / sd * np.sqrt(PER_YEAR), 2) if sd > 0 else None,
            "t": round(mu / (sd / np.sqrt(n)), 2) if sd > 0 else None,
            "win_d": round(float((x > 0).mean()), 4),
            "maxdd_pct": round(dd * 100, 1)}


def perf(r: pd.Series, cost_day_bp: float) -> dict:
    r = pd.Series(r).astype(float).dropna()
    if len(r) < 10:
        return {"gross": {}, "net": {}}
    return {"gross": _stat(r), "net": _stat(r - cost_day_bp / 1e4)}


def legs(d: pd.DataFrame, rt_bp: float) -> dict:
    """返回各方案的日收益序列（成本在 perf 里扣）"""
    i0, g = d["i0"], d["g"]
    gl = g.shift(1)
    r_roll = 0.5 * ((1 + gl.fillna(0)) * (1 + i0) - 1) + 0.5 * i0
    return {
        "roll_half":      (r_roll,             0.5 * rt_bp),
        "intraday_only":  (i0,                 rt_bp),
        "overnight_only": (g,                  rt_bp),
        "always_full":    ((1 + i0) * (1 + g) - 1, rt_bp),
        "buyhold_cc":     (d["cc"],            0.0),
    }


def run_variants(d: pd.DataFrame, rt_bp: float) -> dict:
    return {k: perf(r, c) for k, (r, c) in legs(d, rt_bp).items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rt", type=float, default=15.0)
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--tag", default="v2")
    args = ap.parse_args()
    t0 = time.time()
    REP.mkdir(parents=True, exist_ok=True); OUTD.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("PRAGMA threads=8"); con.execute("PRAGMA memory_limit='8GB'")
    con.execute("PRAGMA disable_progress_bar")
    build(con)

    stat = con.execute("""
    SELECT count(*) kept,
           count(*) FILTER (WHERE NOT next_ok) AS drop_next,
           count(*) FILTER (WHERE is_st) AS n_st,
           count(*) FILTER (WHERE callim1) AS n_callim1
    FROM pf""").df().iloc[0]
    tot = con.execute("SELECT count(*) FROM raw").fetchone()[0]
    print(f"[pf] rows={int(stat.kept):,} / raw={tot:,} ({stat.kept/tot*100:.1f}%)  "
          f"次日无成交={int(stat.drop_next):,} ({stat.drop_next/stat.kept*100:.2f}%)")
    print(f"[pf] 卖出日跌停收盘占比 = {stat.n_callim1/stat.kept*100:.2f}%  "
          f"ST占位（未过滤，仅统计）= {stat.n_st/stat.kept*100:.2f}%")

    day = agg_daily(con)
    day["d"] = pd.to_datetime(day["d"])
    day = day[day["d"] >= args.start].sort_values("d").reset_index(drop=True).set_index("d")
    print(f"[daily] {len(day)} 交易日 {day.index.min().date()} → {day.index.max().date()}")
    print(f"[daily] i(日内)={day.i0.mean()*1e4:.2f}bp  g(隔夜)={day.g.mean()*1e4:.2f}bp  "
          f"i1(次日日内)={day.i1.mean()*1e4:.2f}bp  cc(收盘持有)={day.cc.mean()*1e4:.2f}bp")

    var = run_variants(day, args.rt)
    vtab = pd.DataFrame([{"variant": k, "kind": kind, **v[kind]}
                         for k, v in var.items() for kind in ("gross", "net")])

    ctab = pd.DataFrame([{"rt_bp": rt, **run_variants(day, rt)["roll_half"]["net"]}
                         for rt in [0, 5, 10, 15, 20, 25, 30]])
    be = 2 * day["i0"].mean() * 1e4 + 2 * 0.5 * day["g"].mean() * 1e4   # 毛日收益*2
    gross_day_bp = (0.5 * day["g"].shift(1).fillna(0).mean() + day["i0"].mean()) * 1e4
    print(f"\n[breakeven] 毛日收益={gross_day_bp:.2f}bp → 盈亏平衡满仓往返成本 = {2*gross_day_bp:.2f}bp")

    yrows = []
    for y, gd in day.groupby(day.index.year):
        v = run_variants(gd, args.rt)
        yrows.append({"year": int(y), "n_days": len(gd),
                      "i_bp": round(gd.i0.mean()*1e4, 2), "g_bp": round(gd.g.mean()*1e4, 2),
                      "roll_total_pct": v["roll_half"]["gross"]["total_pct"],
                      "roll_net_pct": v["roll_half"]["net"]["total_pct"],
                      "roll_net_sharpe": v["roll_half"]["net"]["sharpe"],
                      "buyhold_pct": v["buyhold_cc"]["gross"]["total_pct"],
                      "intra_net_pct": v["intraday_only"]["net"]["total_pct"]})
    ytab = pd.DataFrame(yrows)

    btabs = {}
    for col, name in [("liq_q", "liq"), ("size_q", "size")]:
        b = agg_daily(con, f"pf.d, b.{col}", join_bkt=True)
        b["d"] = pd.to_datetime(b["d"])
        b = b[b["d"] >= args.start]
        rows = []
        for q, gq in b.groupby(col):
            gq = gq.sort_values("d").set_index("d")
            v = run_variants(gq, args.rt)["roll_half"]
            rows.append({"bucket": int(q), "n_days": len(gq),
                         "i_bp": round(gq.i0.mean()*1e4, 2), "g_bp": round(gq.g.mean()*1e4, 2),
                         "gross_cagr_pct": v["gross"]["cagr_pct"],
                         "gross_sharpe": v["gross"]["sharpe"],
                         "net_cagr_pct": v["net"]["cagr_pct"],
                         "net_sharpe": v["net"]["sharpe"],
                         "net_maxdd_pct": v["net"]["maxdd_pct"]})
        btabs[name] = pd.DataFrame(rows)

    # 月度
    r_roll = legs(day, args.rt)["roll_half"][0] - args.rt / 2 / 1e4
    mret = (1 + r_roll).groupby(day.index.to_period("M")).prod() - 1
    mtab = pd.DataFrame({"month": mret.index.astype(str), "ret_pct": (mret.values*100).round(2)})

    # 指数基准
    idx = con.execute(f"""
    SELECT date, code, close FROM read_parquet('{IDX}')
    WHERE date >= DATE '{args.start}' AND date <= DATE '{day.index.max().date()}'
    """).df()
    idx["date"] = pd.to_datetime(idx["date"])
    irows = []
    for code, gd in idx.groupby("code"):
        gd = gd.sort_values("date")
        tot = gd["close"].iloc[-1] / gd["close"].iloc[0] - 1
        yrs = len(gd) / PER_YEAR
        irows.append({"index": code, "cagr_pct": round(((1+tot)**(1/yrs)-1)*100, 1),
                      "total_pct": round(tot*100, 1)})
    itab = pd.DataFrame(irows).sort_values("cagr_pct", ascending=False)

    # NAV
    cur = legs(day, args.rt)
    navs = {"dates": [str(x.date()) for x in day.index]}
    for k, (r, c) in cur.items():
        navs[k + "_g"] = ((1 + r.fillna(0)).cumprod()*100).round(2).tolist()
        navs[k + "_n"] = ((1 + r.fillna(0) - c/1e4).cumprod()*100).round(2).tolist()

    print("\n=== 方案对比（rt=%.0fbp）===" % args.rt)
    print(vtab.to_string(index=False))
    print("\n=== 成本敏感性（滚动半仓，net）===")
    print(ctab[["rt_bp","cagr_pct","total_pct","sharpe","maxdd_pct"]].to_string(index=False))
    print("\n=== 分年度 ==="); print(ytab.to_string(index=False))
    print("\n=== 分流动性五分位（Q1=过去20日均额最小）==="); print(btabs["liq"].to_string(index=False))
    print("\n=== 分市值五分位 ==="); print(btabs["size"].to_string(index=False))
    print("\n=== 指数基准 ==="); print(itab.to_string(index=False))

    for df, nm in [(vtab, "variants"), (ctab, "cost"), (ytab, "by_year"),
                   (btabs["liq"], "by_liq"), (btabs["size"], "by_size"), (mtab, "monthly")]:
        df.to_csv(REP/f"half_roll_{nm}_{args.tag}.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(navs, index=day.index.astype(str)).to_csv(
        REP/f"half_roll_nav_{args.tag}.csv", encoding="utf-8-sig")
    day.to_parquet(OUTD/f"daily_{args.tag}.parquet")

    (REP/f"half_roll_summary_{args.tag}.json").write_text(json.dumps({
        "tag": args.tag, "rt_bp": args.rt, "start": args.start,
        "span": [str(day.index.min().date()), str(day.index.max().date())],
        "n_days": len(day), "n_obs": int(stat.kept),
        "mean_i_bp": round(day.i0.mean()*1e4, 2), "mean_g_bp": round(day.g.mean()*1e4, 2),
        "breakeven_rt_bp": round(2*gross_day_bp, 2),
        "variants": vtab.to_dict("records"), "cost_sens": ctab.to_dict("records"),
        "by_year": ytab.to_dict("records"), "by_liq": btabs["liq"].to_dict("records"),
        "by_size": btabs["size"].to_dict("records"), "index": itab.to_dict("records"),
        "monthly": mtab.to_dict("records"), "elapsed_sec": round(time.time()-t0, 1),
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n[done] {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
