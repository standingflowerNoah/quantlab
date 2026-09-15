#!/usr/bin/env python3
"""tushare 代理单日日线补入主库 kline_daily（fsdb 上游滞后 + TDX 全挂时的兜底）。

用法：
  python scripts/backfill_kline_daily_tushare.py 20260911            # 预览校验
  python scripts/backfill_kline_daily_tushare.py 20260911 --commit   # 落库

数据源：t.xiaodefa.top `daily`（OHLCV，vol 手→股 ×100、amount 千元→元 ×1000）
        + `adj_factor`（当日复权因子；缺失回退主库最近一日）。
写入：Store.upsert 幂等（键 code+date），与 fsdb_fallback 同款 DDL。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))  # quantlab 包本身（Store 导入需要）

from backfill_tushare_datasets import api, to_df  # noqa: E402


def fetch_day(trade_date: str) -> pd.DataFrame:
    """tushare daily + adj_factor → 主库 kline_daily 形状（单位换算后）。

    ⚠️ 主库 adj_factor 语义 = 相对近端增量因子（97.8% 股票为 1.0，仅近期有
    分红/拆分的股票 ≠1），tushare adj_factor = 累计后复权因子，二者不同源。
    正确做法 = 比值传播：主库新因子 = 主库最近因子 × (ts当日/ts前日)。
    """
    f, items = api("daily", {"trade_date": trade_date})
    if not items:
        raise SystemExit(f"代理无 {trade_date} daily 数据")
    df = to_df(f, items)
    n_raw = len(df)
    df["code"] = df["ts_code"].str.split(".").str[0]
    for c in ("open", "high", "low", "close", "vol", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # ── 前一交易日（从镜像交易日序列推） ──
    import duckdb
    con = duckdb.connect()
    prev = con.execute(
        "SELECT max(date) FROM read_parquet("
        "'data/lake/clean/mirror/kline_daily.parquet') WHERE date < ?",
        [pd.Timestamp(trade_date)]).fetchone()[0]
    prev = pd.Timestamp(prev).strftime("%Y%m%d")
    main_prev = con.execute(
        "SELECT code, adj_factor FROM read_parquet("
        "'data/lake/clean/mirror/kline_daily.parquet') WHERE date = ?",
        [pd.Timestamp(prev)]).df()
    con.close()
    main_prev_map = dict(zip(main_prev["code"], main_prev["adj_factor"]))
    print(f"前一日 {prev}；主库最近因子覆盖 {len(main_prev_map)} 只（镜像）")

    # ── tushare 两日累计因子 → 比值 ──
    fa, ia = api("adj_factor", {"trade_date": trade_date})
    ts_now = to_df(fa, ia) if ia else pd.DataFrame()
    fa2, ia2 = api("adj_factor", {"trade_date": prev})
    ts_prev = to_df(fa2, ia2) if ia2 else pd.DataFrame()
    now_map = dict(zip(ts_now["ts_code"].str.split(".").str[0],
                       pd.to_numeric(ts_now["adj_factor"], errors="coerce")))
    prev_map = dict(zip(ts_prev["ts_code"].str.split(".").str[0],
                        pd.to_numeric(ts_prev["adj_factor"], errors="coerce")))
    codes = df["code"]
    # 全程 RangeIndex 位置对齐（勿用代码字符串做 Series 索引，会外连接翻倍）
    now_f = codes.map(now_map).astype(float)
    prev_f = codes.map(prev_map).astype(float)
    ratio = (now_f / prev_f).where(
        now_f.notna() & prev_f.notna() & (prev_f != 0), 1.0)
    base = codes.map(main_prev_map).astype(float).fillna(1.0)
    adj_final = base * ratio
    n_act = int((ratio != 1.0).sum())
    print(f"tushare 因子比值≠1（当日有除权动作）{n_act} 只 → 这些股票因子将更新")

    out = pd.DataFrame({
        "date": pd.to_datetime(df["trade_date"], format="%Y%m%d"),
        "code": codes,
        "open": df["open"], "high": df["high"], "low": df["low"],
        "close": df["close"],
        "vol": df["vol"] * 100.0,        # 手 → 股
        "amount": df["amount"] * 1000.0,  # 千元 → 元
        "adj_factor": adj_final,
    })
    assert len(out) == n_raw, f"行数异常 {len(out)} != {n_raw}"
    bad_ohlc = int(((out[["open", "high", "low", "close"]] <= 0)
                    | out["close"].isna()).any(axis=1).sum())
    # 停牌/占位 0 价行剔除（fsdb 惯例：无数据不落行）
    out = out[~((out[["open", "high", "low", "close"]] <= 0)
                | out["close"].isna()).any(axis=1)]
    print(f"代理返回 {n_raw} 行；剔停牌/占位 {bad_ohlc} 行 → {len(out)} 行")
    return out


def main() -> None:
    td = sys.argv[1]
    commit = "--commit" in sys.argv
    df = fetch_day(td)

    # ── 校验 ────────────────────────────────────────────────
    if df.empty:
        raise SystemExit("空数据，中止")
    q = df["close"].quantile([0.01, 0.5, 0.99])
    print(f"close 分位 1%={q.iloc[0]:.2f} 中位={q.iloc[1]:.2f} 99%={q.iloc[2]:.2f}")
    neg = int((df[["open", "high", "low", "close"]] <= 0).any(axis=1).sum())
    print(f"价 ≤0 行数: {neg}（保留，异常源数据需人工看）")
    if neg:
        print(df[(df[["open", "high", "low", "close"]] <= 0).any(axis=1)].head())

    if not commit:
        print("预览模式（--commit 落库）")
        return

    from quantlab.data.store import Store
    from quantlab.data.sources.fsdb_fallback import KLINE_DDL
    store = Store()
    store.ensure_table("kline_daily", KLINE_DDL)

    # adj_factor 回退：主库最近一日的因子
    need = df["adj_factor"].isna()
    if need.any():
        last = store.q(
            "SELECT code, adj_factor FROM kline_daily "
            "WHERE date = (SELECT max(date) FROM kline_daily)")
        m = dict(zip(last["code"], last["adj_factor"]))
        df.loc[need, "adj_factor"] = df.loc[need, "code"].map(m).fillna(1.0)
        print(f"adj_factor 回退填 {int(need.sum())} 行")

    n = store.upsert(df, "kline_daily", ["code", "date"])
    wm = store.q("SELECT max(date) FROM kline_daily").iloc[0, 0]
    store.set_watermark("kline_daily", wm)
    chk = store.q(
        "SELECT count(*) n, min(close) mn, max(close) mx FROM kline_daily "
        "WHERE date = ?", [pd.to_datetime(td)])
    print(f"upsert {n} 行；库内 {td} 共 {int(chk['n'][0])} 行，"
          f"close [{chk['mn'][0]:.2f}, {chk['mx'][0]:.2f}]，水位 {wm}")
    store.close()


if __name__ == "__main__":
    main()
