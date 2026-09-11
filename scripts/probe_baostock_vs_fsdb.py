"""baostock 5min 与 fsdb 1min（湖）重叠期对账
=================================================
目的：判断 baostock 的 5 分钟历史（2020 起）能否与现有 1 分钟湖拼接使用。
方法：取重叠期（2025-03）同一批股票，两边各自按日聚合 OHLCV/额，比对相对误差。
用法: python scripts/probe_baostock_vs_fsdb.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402

from quantlab import config  # noqa: E402

PAIRS = [("sh.600000", "600000"), ("sz.000001", "000001"),
         ("sz.300750", "300750"), ("sh.601318", "601318")]
START, END = "2025-03-03", "2025-03-14"


def lake_daily() -> pd.DataFrame:
    d = config.KLINE_1MIN_DIR
    # 只扫命中分区（全湖 glob 会遍历 2000+ 文件统计量，慢）
    glob_pat = str(d / f"year={START[:4]}" / "part-*.parquet").replace("\\", "/")
    codes = ",".join(f"'{c}'" for _, c in PAIRS)
    con = duckdb.connect()
    return con.execute(f"""
        SELECT code, CAST(datetime AS DATE) AS date,
               first(open ORDER BY datetime)  AS o,
               max(high) AS h, min(low) AS l,
               last(close ORDER BY datetime)  AS c,
               sum(vol) AS v, sum(amount) AS a
        FROM read_parquet('{glob_pat}', hive_partitioning=false)
        WHERE code IN ({codes}) AND datetime >= ? AND datetime < ?
        GROUP BY 1, 2 ORDER BY 1, 2
    """, [pd.Timestamp(START), pd.Timestamp(END) + pd.Timedelta(days=1)]).df()


def bs_daily() -> pd.DataFrame:
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
    rows = []
    for bc, code in PAIRS:
        rs = bs.query_history_k_data_plus(
            bc, "date,time,open,high,low,close,volume,amount",
            start_date=START, end_date=END, frequency="5", adjustflag="3")
        if rs.error_code != "0":
            print(f"  [!] {bc} err={rs.error_code} {rs.error_msg}")
            continue
        while rs.next():
            r = rs.get_row_data()
            rows.append({"code": code, "date": r[0][:10], "bar": r[1][8:12],
                         "open": float(r[2]), "high": float(r[3]),
                         "low": float(r[4]), "close": float(r[5]),
                         "vol": float(r[6]), "amount": float(r[7])})
    bs.logout()
    b = pd.DataFrame(rows)
    if b.empty:
        return b
    return (b.groupby(["code", "date"], as_index=False)
             .agg(o=("open", "first"), h=("high", "max"), l=("low", "min"),
                  c=("close", "last"), v=("vol", "sum"), a=("amount", "sum")))


def main():
    lk = lake_daily()
    bao = bs_daily()
    if lk.empty or bao.empty:
        print("数据为空，无法对账")
        return
    lk["date"] = lk["date"].astype(str)
    print(f"湖 fsdb 1min 日聚合: {len(lk)} 行 | baostock 5min 日聚合: {len(bao)} 行")
    m = lk.merge(bao, on=["code", "date"], suffixes=("_fsdb", "_bao"))
    print(f"可配对: {len(m)} 股日\n")
    for k in ["o", "h", "l", "c", "v", "a"]:
        err = (m[f"{k}_bao"] - m[f"{k}_fsdb"]).abs() / \
              m[f"{k}_fsdb"].abs().clip(lower=1e-9)
        print(f"  {k:<3} 中位相对误差 {err.median():.5%}   p95 {err.quantile(.95):.5%}   "
              f"max {err.max():.4%}")
    print("\n明细（收盘价 / 成交量对比）：")
    show = m[["code", "date", "c_fsdb", "c_bao", "v_fsdb", "v_bao"]].copy()
    show["vol_err"] = (show["v_bao"] - show["v_fsdb"]).abs() / show["v_fsdb"]
    print(show.to_string(index=False))


if __name__ == "__main__":
    main()
