"""baostock 能力体检：起点 / 覆盖 / 拉取速度
================================================
回答"能不能用 baostock 把分钟层向前补到 2020"需要的三个事实：
1) 5 分钟线最早到哪一年（按月抽样定位）
2) 覆盖哪些市场（sh/sz/bj、个股/指数）
3) 单股 5 年拉取耗时（估算全市场回补总时长）
用法: python scripts/probe_baostock_capability.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import baostock as bs  # noqa: E402

FIELDS = "date,time,code,open,high,low,close,volume,amount"


def rows_of(code, freq, start, end) -> int:
    rs = bs.query_history_k_data_plus(code, FIELDS, start_date=start,
                                      end_date=end, frequency=freq,
                                      adjustflag="3")
    if rs.error_code != "0":
        return -1
    return len(rs.get_data()) if hasattr(rs, "get_data") else sum(
        1 for _ in iter(rs.next, False))


def main():
    lg = bs.login()
    print(f"login: {lg.error_code}")

    print("\n[1] 5 分钟起点定位（sh.600000，按月抽样）")
    for s, e in [("2019-09-01", "2019-09-30"), ("2019-10-01", "2019-10-31"),
                 ("2019-11-01", "2019-11-30"), ("2019-12-01", "2019-12-31"),
                 ("2020-01-01", "2020-01-31"), ("2020-02-01", "2020-02-29")]:
        n = rows_of("sh.600000", "5", s, e)
        print(f"   {s[:7]}: {n} 行{'' if n else '   <-- 空'}")

    print("\n[2] 覆盖范围（2024-06-03~06-07 一周 5 分钟）")
    for bc, lbl in [("sh.600000", "沪主板"), ("sz.000001", "深主板"),
                    ("sz.300750", "创业板"), ("sh.688001", "科创板"),
                    ("sh.601318", "沪主板2"), ("bj.430047", "北交所"),
                    ("sh.000300", "指数:沪深300"), ("sz.399006", "指数:创业板指")]:
        n = rows_of(bc, "5", "2024-06-03", "2024-06-07")
        print(f"   {bc:<12}{lbl:<12}: {n} 行")

    print("\n[3] 拉取速度（sh.600000，5 分钟 2020-2024 分年拉）")
    t0 = time.time()
    tot = 0
    for y in range(2020, 2025):
        n = rows_of("sh.600000", "5", f"{y}-01-01", f"{y}-12-31")
        tot += max(n, 0)
    el = time.time() - t0
    print(f"   合计 {tot:,} 行 / {el:.1f}s（单股 5 年）")
    print(f"   → 5000 只串行 ≈ {el * 5000 / 3600:.1f} 小时")

    print("\n[4] 全市场证券数（最新交易日）")
    cal = bs.query_trade_dates(start_date="2026-09-01", end_date="2026-09-11")
    days = [r[0] for r in cal.get_data().values.tolist() if r[1] == "1"]
    if days:
        allst = bs.query_all_stock(day=days[-1])
        df = allst.get_data()
        print(f"   {days[-1]}: 共 {len(df)} 只；按前缀统计")
        pfx = df["code"].str.split(".").str[0].value_counts().to_dict()
        print(f"   {pfx}")
    bs.logout()


if __name__ == "__main__":
    main()
