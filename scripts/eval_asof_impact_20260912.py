"""as-of 口径切换的影响评估：新旧因子 IC 对比 + 生产模型回测对比

两部分：
  Part 1（无需写锁）：size/turnover/total_mcap 新旧口径的 20 日前瞻 Rank IC
          —— 回答「新口径的因子有效性有没有变差」
  Part 2（需写锁）：生产模型 PROD = size+amihud_20 等权 top100/20日/inverse_vol
          全期回测，新旧口径各跑一遍，回答「年化/夏普/回撤变了多少」

输出: reports/asof_impact_20260912.json + 控制台表格
"""
from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
NEW = "data/lake/factor"
OLD = "data/backup_factors_20260911"
KLINE = "data/lake/clean/mirror/kline_daily.parquet"
OUT = ROOT / "reports" / "asof_impact_20260912.json"

FULL_START = "2022-07-01"
END = "2026-09-11"


# ─────────────────────────── Part 1: IC 对比 ───────────────────────────
def part1_ic(con: duckdb.DuckDBPyConnection) -> dict:
    from quantlab.data.universe import get_universe
    codes = get_universe("ashare_ex")
    con.execute("CREATE OR REPLACE TABLE u(code VARCHAR)")
    con.executemany("INSERT INTO u VALUES (?)", [(c,) for c in codes])

    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE fwd AS
        SELECT date, code, c_lead / c - 1 AS fwd FROM (
            SELECT date, code, close * adj_factor AS c,
                   LEAD(close * adj_factor, 20) OVER (
                       PARTITION BY code ORDER BY date) AS c_lead
            FROM read_parquet('{KLINE}'))
        WHERE c_lead IS NOT NULL AND c > 0
    """)

    def ic_of(path_glob: str, factor: str) -> dict:
        q = f"""
        WITH j AS (
            SELECT CAST(f.date AS DATE) d, f.code, f.value, k.fwd
            FROM read_parquet('{path_glob}') f
            JOIN fwd k ON k.date = f.date AND k.code = f.code
            WHERE f.code IN (SELECT code FROM u)
        ),
        rk AS (
            SELECT d,
                   rank() OVER (PARTITION BY d ORDER BY value) rv,
                   rank() OVER (PARTITION BY d ORDER BY fwd)  rf
            FROM j
        ),
        r AS (
            SELECT d, corr(rv, rf) AS rk_ic FROM rk GROUP BY d
        )
        SELECT avg(rk_ic) ic, stddev_samp(rk_ic) sd, count(*) n_days
        FROM r WHERE rk_ic IS NOT NULL
          AND d >= DATE '{FULL_START}' AND d <= DATE '{END}'
        """
        row = con.execute(q).fetchone()
        ic, sd, n = row[0], row[1], row[2]
        return {"ic20": round(float(ic), 5) if ic is not None else None,
                "icir": round(float(ic / sd), 3) if (ic is not None and sd) else None,
                "n_days": int(n)}

    out = {}
    for f in ["size", "turnover", "total_mcap"]:
        out[f] = {"new": ic_of(f"{NEW}/{f}/part-*.parquet", f),
                  "old": ic_of(f"{OLD}/{f}/part-*.parquet", f)}
        n, o = out[f]["new"], out[f]["old"]
        print(f"  {f:12s} 新 IC={n['ic20']:+.4f}(ICIR {n['icir']})  "
              f"旧 IC={o['ic20']:+.4f}(ICIR {o['icir']})  "
              f"ΔIC={n['ic20']-o['ic20']:+.5f}")
    return out


# ─────────────────── Part 2: 生产模型回测 新 vs 旧 ───────────────────
def part2_backtest(res: dict) -> dict:
    # 等主库写锁（本机常有并发工作负载）
    import duckdb as _dq
    from quantlab import config as _cfg
    _db = str(_cfg.DUCKDB_PATH)
    for i in range(240):
        try:
            _c = _dq.connect(_db)
            _c.execute("SELECT 1").fetchone()
            _c.close()
            print(f"[lock] 拿到写锁（第 {i+1} 次尝试）")
            break
        except Exception:
            time.sleep(15)
    else:
        raise SystemExit("等待写锁超时")

    from quantlab.model import build_composite
    from quantlab.optimize.backtest import run_optimized_backtest
    import quantlab.model.composite as C
    from quantlab.factor import registry as R

    def run_with_size(size_glob: str, tag: str) -> dict:
        """临时把 size 的读取指向指定 parquet，跑完恢复"""
        orig_read = None
        # build_composite → store.read_factor('size')；用 monkeypatch 指向备份目录
        from quantlab.data.store import Store
        store = Store()
        orig = store.read_factor

        def patched(factor_name: str, start=None, end=None):
            if factor_name == "size":
                d = duckdb.connect(":memory:")
                df = d.execute(
                    f"SELECT * FROM read_parquet('{size_glob}')").df()
                d.close()
                df["date"] = pd.to_datetime(df["date"])
                if start is not None:
                    df = df[df["date"] >= pd.to_datetime(start)]
                if end is not None:
                    df = df[df["date"] <= pd.to_datetime(end)]
                return df
            return orig(factor_name, start, end)

        store.read_factor = patched
        try:
            score = build_composite(["size", "amihud_20"],
                                    universe="ashare_ex", start=FULL_START)
            r = run_optimized_backtest(score.copy(), n_stocks=100, rebalance=20,
                                       method="inverse_vol",
                                       start=FULL_START, end=END)
        finally:
            store.read_factor = orig
        m, ex = r["metrics"], r["excess"]
        c = r["curve"].copy()
        c["year"] = pd.to_datetime(c["date"]).dt.year
        yearly = {int(y): round(float((1 + g["ret"]).prod() - 1), 4)
                  for y, g in c.groupby("year")}
        print(f"  [{tag}] 年化 {m['annual_return']*100:.1f}% / 夏普 {m['sharpe']:.2f}"
              f" / 回撤 {m['max_drawdown']*100:.1f}% / 超额 {ex['excess_annual']*100:.1f}%"
              f" / 换手 {r['turnover_avg']*100:.0f}%")
        return {"annual": round(m["annual_return"], 4),
                "sharpe": round(m["sharpe"], 3),
                "mdd": round(m["max_drawdown"], 4),
                "excess": round(ex["excess_annual"], 4),
                "ir": round(ex["information_ratio"], 3),
                "turnover": round(r["turnover_avg"], 4),
                "yearly": yearly}

    out = {}
    t0 = time.time()
    print("\nPart 2 生产模型回测（新口径）...")
    out["new"] = run_with_size(f"{NEW}/size/part-*.parquet", "新口径")
    print("Part 2 生产模型回测（旧口径，备份数据）...")
    out["old"] = run_with_size(f"{OLD}/size/part-*.parquet", "旧口径")
    print(f"  （耗时 {time.time()-t0:.0f}s）")
    return out


def main() -> None:
    only = sys.argv[1] if len(sys.argv) > 1 else "all"
    res: dict = {}
    if OUT.exists():                      # 分步运行时保留已算好的部分
        try:
            res = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            res = {}
    res["generated"] = pd.Timestamp.now().isoformat(timespec="seconds")
    res["window"] = f"{FULL_START}~{END}"
    con = duckdb.connect(":memory:")

    if only in ("all", "ic"):
        print("Part 1 因子 IC 新旧对比（ashare_ex，20 日前瞻 Rank IC）")
        res["ic"] = part1_ic(con)
    con.close()

    if only in ("all", "bt"):
        res["backtest"] = part2_backtest(res)
        n, o = res["backtest"]["new"], res["backtest"]["old"]
        print("\n=== 回测对比 ===")
        print(f"  年化   {o['annual']*100:6.1f}% → {n['annual']*100:6.1f}%")
        print(f"  夏普   {o['sharpe']:6.2f} → {n['sharpe']:6.2f}")
        print(f"  回撤   {o['mdd']*100:6.1f}% → {n['mdd']*100:6.1f}%")
        print(f"  超额   {o['excess']*100:6.1f}% → {n['excess']*100:6.1f}%")

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n已写入 {OUT}")


if __name__ == "__main__":
    main()
