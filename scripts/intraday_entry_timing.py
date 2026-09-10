r"""A股「每日何时买入收益最高」研究 —— 买入时点收益曲线
=================================================================
问题：T 日在盘中某一分钟 m（按其 close 价）买入，持有到 T+1 开盘卖出，
      哪个 m 的平均收益最高？

收益定义：
    ret_m = adj_next_open / close_m(T) - 1
    adj_next_open = next_open*(1+N+R) + D - R*S   （除权事件挂在【卖出日】T+1）
      N=songzhuangu/股, R=peigu/股, D=fenhong/股, S=peigujia

分解（恒等）：
    ret_m = [ close_T/close_m - 1 ]  +  [ adj_next_open/close_T - 1 ]
             (日内剩余段，随 m 变化)      (隔夜段，与 m 无关)

关键时点：
    m = 09:25 集合竞价（用日线 open 买入）= 理论最早可成交时点
    m = 09:30 bar close ≈ 09:31 价
    m = 15:00 bar close = 日线收盘价（收盘集合竞价）

数据：分钟层 kline_1min 2025-01-02 起（fsdb 源）；日线 daily_segments 2022 起。
      15:01-15:30 为北交所零成交占位 bar，全部剔除；北交所/停牌/买不到剔除。
统计口径：逐日横截面等权均值的时间序列（t 值按日序列算），非 pool 全样本。

输出：
    data/lake/factor/overnight/entry_timing/cell_main.parquet       (d,mod,bucket) 聚合
    data/lake/factor/overnight/entry_timing/curve_daily_ext.parquet 日线外延 2022-2026
    reports/overnight/entry_timing_*.csv / .json
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
DIV = str(ROOT / "data/lake/clean/mirror/dividend_events.parquet").replace("\\", "/")
SEG = str(ROOT / "data/lake/factor/overnight/daily_segments/*.parquet").replace("\\", "/")
FSDB = str(ROOT / "data/lake/factor/overnight/daily_fsdb/*.parquet").replace("\\", "/")
MN = str(ROOT / "data/lake/clean/kline_1min/year={yr}/*.parquet").replace("\\", "/")
OUT = ROOT / "data/lake/factor/overnight/entry_timing"
REP = ROOT / "reports/overnight"

MOD_OPEN = 569          # 伪时点：开盘集合竞价
MOD_FIRST = 571         # 09:31（09:30 bar 的 close）
MOD_CLOSE = 900         # 15:00 bar 的 close = 日线收盘
# ⚠️ mod 780（13:00）是污染 bar：2026 年仅覆盖约 90 只 688 段次新/高价股的部分
#    交易日，价格与前后 bar 脱节，使该时点日均收益虚高到 +222bp（t=12.2）。
#    实测全样本加权均值仅 −1.6bp → 整表剔除，否则会污染"最优买入时点"排序。
BAD_MODS = (780,)
ALL_DAYS = {"d", "mod"}


def hm(mod: int) -> str:
    if mod == MOD_OPEN:
        return "09:25"
    return f"{mod // 60:02d}:{mod % 60:02d}"


# ──────────────────────────────────────────────────────────────
# 1. 下一开盘价（除权调整）+ 股票池属性
# ──────────────────────────────────────────────────────────────
NX_SQL = r"""
CREATE OR REPLACE TEMP TABLE seg AS
SELECT code, date, open, close, prev_close, vol, amount,
       COALESCE(close_at_limit, FALSE) AS close_at_limit,
       COALESCE(open_at_limit,  FALSE) AS open_at_limit
FROM read_parquet('{seg}')
WHERE date >= DATE '2021-11-01';

CREATE OR REPLACE TEMP TABLE cal AS
SELECT dt, LEAD(dt) OVER (ORDER BY dt) AS nd
FROM (SELECT DISTINCT date AS dt FROM seg);

CREATE OR REPLACE TEMP TABLE dd AS
SELECT code, date, SUM(fenhong) AS fh, SUM(songzhuangu) AS sz, SUM(peigu) AS pg,
       CASE WHEN SUM(peigu) > 0 THEN SUM(peigu*peigujia)/SUM(peigu) ELSE 0 END AS pj
FROM read_parquet('{div}')
WHERE date >= DATE '2021-11-01'
GROUP BY code, date;

CREATE OR REPLACE TEMP TABLE nx AS
SELECT s.code, s.date AS d, c.nd AS d1,
       s.open AS open_t, s.close AS close_t, s.prev_close,
       s.vol AS vol_t, s.amount AS amt_t,
       o.open AS next_open, o.vol AS next_vol,
       CASE WHEN s.prev_close > 0 THEN s.close / s.prev_close - 1 END AS ret_t,
       CASE WHEN e.code IS NULL THEN o.open
            ELSE o.open*(1 + COALESCE(e.sz,0) + COALESCE(e.pg,0))
                 + COALESCE(e.fh,0) - COALESCE(e.pg,0)*COALESCE(e.pj,0)
       END AS next_open_adj,
       CASE
         WHEN substr(s.code,1,3) = '688' OR substr(s.code,1,3) = '300' THEN 0.20
         WHEN substr(s.code,1,1) = '3' THEN 0.20
         WHEN substr(s.code,1,1) = '8' OR substr(s.code,1,3) = '920' THEN 0.30
         ELSE 0.10
       END AS limit_pct,
       f.is_st, f.float_mv,
       CASE WHEN o.open IS NULL OR o.vol IS NULL OR o.vol <= 0 THEN FALSE ELSE TRUE END AS sellable
FROM seg s
JOIN cal c ON c.dt = s.date
LEFT JOIN seg o ON o.code = s.code AND o.date = c.nd
LEFT JOIN dd  e ON e.code = s.code AND e.date = c.nd
LEFT JOIN read_parquet('{fsdb}') f ON f.code = s.code AND f.date = s.date
WHERE s.date >= DATE '2022-01-01';
"""

def uf(alias: str = "n") -> str:
    return (f"substr({alias}.code,1,1) NOT IN ('4','8') AND substr({alias}.code,1,3) <> '920' "
            f"AND {alias}.sellable AND {alias}.next_open_adj > 0 AND {alias}.prev_close > 0")


UF = uf("n")


def build(con) -> None:
    for stmt in NX_SQL.format(seg=SEG, div=DIV, fsdb=FSDB).split(";"):
        if stmt.strip():
            con.execute(stmt)
    # 逐日五分位桶（市值 / 成交额），由 nx 自身算，避免在分钟大表上开窗
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE bkt AS
    SELECT code, d,
           ntile(5) OVER (PARTITION BY d ORDER BY float_mv) AS size_q,
           ntile(5) OVER (PARTITION BY d ORDER BY amt_t)    AS liq_q
    FROM nx WHERE {uf('nx')} AND float_mv IS NOT NULL AND float_mv > 0 AND amt_t > 0
    """)


# ──────────────────────────────────────────────────────────────
# 2. 分钟层：库内聚合到 (d, mod, size_q, liq_q, buy_lim)
# ──────────────────────────────────────────────────────────────
def run_year(con, yr: int, strict: bool) -> pd.DataFrame:
    pat = MN.format(yr=yr)
    uf = UF + (" AND COALESCE(n.is_st, FALSE) = FALSE" if strict else "")
    t = time.time()
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE cell_{yr} AS
    WITH j AS (
      SELECT CAST(k.datetime AS DATE) AS d,
             (extract(hour from k.datetime)::INT*60 + extract(minute from k.datetime)::INT) AS mod,
             b.size_q, b.liq_q,
             n.next_open_adj / k.close - 1     AS r,
             n.close_t / k.close - 1           AS ri,
             n.next_open_adj / n.close_t - 1   AS rg,
             CASE WHEN k.close >= n.prev_close*(1+n.limit_pct) - 0.005
                  THEN TRUE ELSE FALSE END    AS buy_lim
      FROM read_parquet('{pat}') k
      JOIN nx n ON n.code = k.code AND n.d = CAST(k.datetime AS DATE)
      LEFT JOIN bkt b ON b.code = k.code AND b.d = n.d
      WHERE k.vol > 0 AND k.close > 0 AND k.open > 0
        AND (extract(hour from k.datetime)::INT*60 + extract(minute from k.datetime)::INT)
            BETWEEN {MOD_FIRST} AND {MOD_CLOSE}
        AND (extract(hour from k.datetime)::INT*60 + extract(minute from k.datetime)::INT)
            NOT IN ({",".join(str(m) for m in BAD_MODS)})
        AND {uf} AND n.close_t > 0
    )
    SELECT d, mod, size_q, liq_q, buy_lim,
           count(*)                       AS n,
           sum(r)                         AS s_r,
           sum(r*r)                       AS s_r2,
           sum(CASE WHEN r > 0 THEN 1 ELSE 0 END) AS n_win,
           sum(ri)                        AS s_intra,
           sum(rg)                        AS s_gap
    FROM j GROUP BY 1,2,3,4,5
    """)
    df = con.execute(f"SELECT * FROM cell_{yr}").df()
    print(f"  [year {yr}] cells={len(df):,}  obs={int(df.n.sum()):,}  "
          f"days={df.d.nunique()}  {time.time()-t:.1f}s")
    return df


def run_open_point(con, yr: int, strict: bool) -> pd.DataFrame:
    """m = 开盘集合竞价：用日线 open 买入 → T+1 开盘卖（口径与分钟点一致）"""
    pat = MN.format(yr=yr)
    uf = UF + (" AND COALESCE(n.is_st, FALSE) = FALSE" if strict else "")
    return con.execute(f"""
    WITH mn0 AS (
      SELECT DISTINCT CAST(k.datetime AS DATE) AS d, k.code
      FROM read_parquet('{pat}') k
      WHERE k.vol > 0
        AND (extract(hour from k.datetime)::INT*60 + extract(minute from k.datetime)::INT) = 570
    )
    SELECT m.d, {MOD_OPEN} AS mod, b.size_q, b.liq_q,
           CASE WHEN n.open_t >= n.prev_close*(1+n.limit_pct) - 0.005
                THEN TRUE ELSE FALSE END AS buy_lim,
           1 AS n, (n.next_open_adj/n.open_t - 1) AS s_r,
           (n.next_open_adj/n.open_t - 1)*(n.next_open_adj/n.open_t - 1) AS s_r2,
           CASE WHEN n.next_open_adj/n.open_t - 1 > 0 THEN 1 ELSE 0 END AS n_win,
           (n.close_t/n.open_t - 1) AS s_intra,
           (n.next_open_adj/n.close_t - 1) AS s_gap
    FROM mn0 m
    JOIN nx n ON n.code = m.code AND n.d = m.d
    LEFT JOIN bkt b ON b.code = n.code AND b.d = n.d
    WHERE {uf} AND n.open_t > 0
    """).df()


# ──────────────────────────────────────────────────────────────
# 3. 聚合与统计
# ──────────────────────────────────────────────────────────────
def merge_cells(df: pd.DataFrame, keys) -> pd.DataFrame:
    g = df.groupby(keys, observed=True).agg(
        n=("n", "sum"), s_r=("s_r", "sum"), s_r2=("s_r2", "sum"),
        n_win=("n_win", "sum"), s_intra=("s_intra", "sum"), s_gap=("s_gap", "sum")
    ).reset_index()
    g["mean_ret"] = g.s_r / g.n
    var = (g.s_r2 - g.s_r**2 / g.n) / (g.n - 1).clip(lower=1)
    g["sd_ret"] = np.sqrt(var.clip(lower=0))
    g["winr"] = g.n_win / g.n
    g["mean_intra"] = g.s_intra / g.n
    g["mean_gap"] = g.s_gap / g.n
    return g


def stats_ts(series: pd.Series, per_year: int = 244) -> dict:
    s = pd.Series(series).dropna()
    n = len(s)
    if n < 5:
        return {"n_days": int(n), "mean_bp": np.nan, "t": np.nan,
                "win": np.nan, "ann_pct": np.nan, "sd_bp": np.nan}
    mu, sd = float(s.mean()), float(s.std(ddof=1))
    return {
        "n_days": int(n),
        "mean_bp": round(mu * 1e4, 2),
        "sd_bp": round(sd * 1e4, 1),
        "t": round(mu / (sd / np.sqrt(n)), 2) if sd > 0 else np.nan,
        "win": round(float((s > 0).mean()), 4),
        "ann_pct": round(((1 + mu) ** per_year - 1) * 100, 1),
    }


def summarize(curve: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for mod, g in curve.groupby("mod"):
        rows.append({"mod": int(mod), "hm": hm(int(mod)),
                     **stats_ts(g.set_index("d")["mean_ret"])})
    return pd.DataFrame(rows).sort_values("mod").reset_index(drop=True)


# ──────────────────────────────────────────────────────────────
# 4. 日线外延：2022-2026，开盘买 / 收盘买
# ──────────────────────────────────────────────────────────────
def run_daily_ext(con) -> pd.DataFrame:
    df = con.execute(f"""
    SELECT n.d, n.next_open_adj/n.open_t - 1 AS r_open,
           n.next_open_adj/n.close_t - 1 AS r_close,
           n.close_t/n.open_t - 1 AS intraday,
           n.open_t/n.prev_close - 1 AS overnight_on_T
    FROM nx n
    WHERE {UF} AND n.vol_t > 0 AND n.open_t > 0 AND n.close_t > 0
    """).df()
    df["d"] = pd.to_datetime(df["d"])
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2025,2026")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--tag", default="main")
    args = ap.parse_args()
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    REP.mkdir(parents=True, exist_ok=True)
    years = [int(y) for y in args.years.split(",")]

    con = duckdb.connect()
    con.execute("PRAGMA threads=8")
    con.execute("PRAGMA memory_limit='8GB'")
    build(con)
    print("[nx]", con.execute(
        "SELECT count(*) FROM nx WHERE " + uf("nx")).fetchone())

    parts = []
    for yr in years:
        parts.append(run_year(con, yr, args.strict))
        parts.append(run_open_point(con, yr, args.strict))
    cell = pd.concat(parts, ignore_index=True)
    cell["d"] = pd.to_datetime(cell["d"])
    cell.to_parquet(OUT / f"cell_{args.tag}.parquet", index=False)
    print(f"[cell] rows={len(cell):,} obs={int(cell.n.sum()):,} "
          f"days={cell.d.nunique()} span {cell.d.min().date()}→{cell.d.max().date()}")

    # ── 主曲线（全样本，含买入时涨停） ──
    curve_all = merge_cells(cell, ["d", "mod"])
    s_all = summarize(curve_all)
    # ── 剔除买入时点涨停封板 ──
    curve = merge_cells(cell[~cell.buy_lim], ["d", "mod"])
    s = summarize(curve)
    s.to_csv(REP / f"entry_timing_curve_{args.tag}.csv", index=False, encoding="utf-8-sig")

    print("\n=== TOP 10 买入时点（剔涨停，2025+）===")
    print(s.sort_values("mean_bp", ascending=False).head(10).to_string(index=False))
    print("\n=== BOTTOM 10 ===")
    print(s.sort_values("mean_bp").head(10).to_string(index=False))

    keys = [MOD_OPEN, 571, 575, 585, 600, 615, 630, 645, 660, 675, 690,
            781, 790, 810, 830, 850, 866, 875, 880, 890, 895, 897, 899, 900]
    pick = s[s["mod"].isin(keys)].copy()
    print("\n=== 关键时点 ===")
    print(pick.to_string(index=False))

    # ── 分年度 ──
    yrows = []
    for mod in keys:
        g = curve[curve["mod"] == mod]
        for y, gg in g.groupby(g["d"].dt.year):
            yrows.append({"hm": hm(mod), "year": int(y),
                          **stats_ts(gg.set_index("d")["mean_ret"])})
    ytab = pd.DataFrame(yrows)
    print("\n=== 分年度 mean_bp（关键时点）===")
    print(ytab.pivot(index="hm", columns="year", values="mean_bp").to_string())
    ytab.to_csv(REP / f"entry_timing_by_year_{args.tag}.csv", index=False, encoding="utf-8-sig")

    # ── 分桶曲线（市值 / 流动性）──
    cur_nl = cell[~cell.buy_lim]
    btabs = {}
    for col, name in [("size_q", "size"), ("liq_q", "liq")]:
        c = merge_cells(cur_nl[cur_nl[col].notna()], ["d", "mod", col])
        c.to_parquet(OUT / f"curve_by_{name}_{args.tag}.parquet", index=False)
        rows = []
        for q, gq in c.groupby(col):
            for mod in keys:
                gg = gq[gq["mod"] == mod]
                if len(gg):
                    rows.append({"hm": hm(mod), "q": int(q),
                                 **stats_ts(gg.set_index("d")["mean_ret"])})
        btabs[name] = pd.DataFrame(rows)
        print(f"\n=== 分{name}五分位 mean_bp ===")
        print(btabs[name].pivot(index="hm", columns="q", values="mean_bp").to_string())
        btabs[name].to_csv(REP / f"entry_timing_by_{name}_{args.tag}.csv",
                           index=False, encoding="utf-8-sig")

    # ── 日线外延 ──
    ext = run_daily_ext(con)
    ext.to_parquet(OUT / f"curve_daily_ext_{args.tag}.parquet", index=False)
    erows = []
    for y, g in ext.groupby(ext["d"].dt.year):
        erows.append({
            "year": int(y),
            "n_days": int(g.d.nunique()),
            "open_to_nextopen_bp": round(g.groupby("d").r_open.mean().mean()*1e4, 2),
            "t_open": stats_ts(g.groupby("d").r_open.mean())["t"],
            "close_to_nextopen_bp": round(g.groupby("d").r_close.mean().mean()*1e4, 2),
            "t_close": stats_ts(g.groupby("d").r_close.mean())["t"],
            "intraday_bp": round(g.groupby("d").intraday.mean().mean()*1e4, 2),
        })
    etab = pd.DataFrame(erows)
    print("\n=== 日线外延 2022-2026（开盘买 vs 收盘买 → 次日开盘卖）===")
    print(etab.to_string(index=False))
    etab.to_csv(REP / f"entry_timing_daily_ext_{args.tag}.csv",
                index=False, encoding="utf-8-sig")

    summary = {
        "tag": args.tag, "strict": args.strict, "years": years,
        "span": [str(cell.d.min().date()), str(cell.d.max().date())],
        "n_days": int(cell.d.nunique()), "n_obs": int(cell.n.sum()),
        "top10": s.sort_values("mean_bp", ascending=False).head(10).to_dict("records"),
        "bottom10": s.sort_values("mean_bp").head(10).to_dict("records"),
        "key_times": pick.to_dict("records"),
        "by_year": ytab.to_dict("records"),
        "by_size": btabs["size"].to_dict("records"),
        "by_liq": btabs["liq"].to_dict("records"),
        "daily_ext": etab.to_dict("records"),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    (REP / f"entry_timing_summary_{args.tag}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n[done] {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
