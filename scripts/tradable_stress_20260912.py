"""生产模型可成交性压力测试（新口径，2026-09-12）

背景：as-of 口径修正后生产模型 PROD 全期年化由 36.7% 下修至 30.4%。
旧的可成交性压力测试（reports/tradable_stress.json，年化 45.7%）基于旧口径，
须重算。本次实现为可复用脚本（原结果由一次性会话生成，无脚本留档）。

三种口径（同一 PROD 评分、同一引擎参数，只改"可买入集合"）：
  无约束          原样
  剔除封板        建仓日已封涨停（板别感知：主板 10%、创业/科创 20%、北交所 30%）
  剔除大涨>7%     建仓日涨幅 > 7%

实现：回测引擎按「score 当日截面 top-n」选股，故直接从 score 里
剔除不符合可成交条件的 (date, code) 即可，不改引擎。

输出: reports/tradable_stress_20260912.json
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
OUT = ROOT / "reports" / "tradable_stress_20260912.json"
START = "2022-07-01"
END = "2026-09-11"


def wait_lock(max_tries: int = 240) -> None:
    from quantlab import config
    db = str(config.DUCKDB_PATH)
    for i in range(max_tries):
        try:
            c = duckdb.connect(db)
            c.execute("SELECT 1").fetchone()
            c.close()
            return
        except Exception:
            time.sleep(15)
    raise SystemExit("等待写锁超时")


def limit_flags(con: duckdb.DuckDBPyConnection | None) -> pd.DataFrame:
    """板别感知的封板 / 大涨标记（前收盘自算，不用来源 pre_close）"""
    if con is None:
        con = duckdb.connect(":memory:")
    return con.execute(f"""
        WITH px AS (
            SELECT code, date, close,
                   LAG(close) OVER (PARTITION BY code ORDER BY date) AS pc
            FROM read_parquet('data/lake/clean/mirror/kline_daily.parquet')
        ), f AS (
            SELECT code, date,
                   CASE WHEN code LIKE '300%' OR code LIKE '301%'
                          OR code LIKE '688%' OR code LIKE '689%'
                        THEN 0.20
                        WHEN code LIKE '83%' OR code LIKE '87%'
                          OR code LIKE '43%' OR code LIKE '92%'
                        THEN 0.30
                        ELSE 0.10 END AS lim
            FROM px
        )
        SELECT px.code, px.date,
               (px.close / NULLIF(px.pc, 0) - 1) AS pct_chg,
               f.lim,
               (px.close / NULLIF(px.pc, 0) - 1) >= f.lim * 0.998 AS sealed,
               (px.close / NULLIF(px.pc, 0) - 1) > 0.07          AS big_up
        FROM px JOIN f ON f.code = px.code AND f.date = px.date
        WHERE px.pc IS NOT NULL AND px.pc > 0
          AND px.date >= DATE '{START}' AND px.date <= DATE '{END}'
    """).df()


def main() -> None:
    wait_lock()
    from quantlab.model import build_composite
    from quantlab.optimize.backtest import run_optimized_backtest

    t0 = time.time()
    score = build_composite(["size", "amihud_20"], universe="ashare_ex",
                            start=START)
    score["date"] = pd.to_datetime(score["date"])
    flags = limit_flags(None)
    flags["date"] = pd.to_datetime(flags["date"])
    print(f"score {len(score):,} 行；flags {len(flags):,} 行（{time.time()-t0:.0f}s）")

    variants = {"无约束(新口径)": score}
    con = duckdb.connect(":memory:")
    con.register("score_df", score)
    con.register("flags_df", flags[["code", "date", "sealed", "big_up"]])
    variants["剔除封板"] = con.execute("""
        SELECT s.date, s.code, s.score FROM score_df s
        ANTI JOIN flags_df f ON f.date = s.date AND f.code = s.code AND f.sealed
    """).df()
    variants["剔除大涨>7%"] = con.execute("""
        SELECT s.date, s.code, s.score FROM score_df s
        ANTI JOIN flags_df f ON f.date = s.date AND f.code = s.code AND f.big_up
    """).df()
    con.close()

    out = {}
    for name, sc in variants.items():
        r = run_optimized_backtest(sc.copy(), n_stocks=100, rebalance=20,
                                   method="inverse_vol", start=START, end=END)
        m, ex = r["metrics"], r["excess"]
        out[name] = {
            "annual": round(m["annual_return"], 4),
            "sharpe": round(m["sharpe"], 3),
            "mdd": round(m["max_drawdown"], 4),
            "excess": round(ex["excess_annual"], 4),
            "ir": round(ex["information_ratio"], 3),
            "turnover": round(r["turnover_avg"], 4),
        }
        print(f"  {name:14s} 年化 {m['annual_return']*100:6.1f}%  "
              f"夏普 {m['sharpe']:5.2f}  回撤 {m['max_drawdown']*100:6.1f}%  "
              f"超额 {ex['excess_annual']*100:6.1f}%  换手 {r['turnover_avg']*100:3.0f}%")

    print(f"\n对照旧口径（reports/tradable_stress.json，已失效）："
          f"无约束 45.7%/1.44、剔除封板 44.9%/1.42")
    OUT.write_text(json.dumps({"generated": pd.Timestamp.now().isoformat(timespec="seconds"),
                               "window": f"{START}~{END}", "results": out},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已写入 {OUT}（{time.time()-t0:.0f}s）")


if __name__ == "__main__":
    main()
