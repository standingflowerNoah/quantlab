"""生产模型 top100 口径位移诊断（旧股本口径 vs 新 PIT 股本口径）

生产模型 = size(-1) + amihud_20(+1) rank 等权，池 = ashare_ex，取 top100。
amihud_20 不依赖股本 → 位移全部来自 size。

旧 size：data/backup_factors_20260911/size
新 size：data/lake/factor/size

只用 parquet + universe 代码表（主库写锁被占时可走镜像，无需抢锁）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb


NEW_SIZE = "data/lake/factor/size/part-*.parquet"
OLD_SIZE = "data/backup_factors_20260911/size/part-*.parquet"
AMIHUD = "data/lake/factor/amihud_20/part-*.parquet"


def main() -> None:
    from quantlab.data.universe import get_universe

    codes = get_universe("ashare_ex")
    print(f"ashare_ex 池规模: {len(codes)} 只")

    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE u(code VARCHAR)")
    con.executemany("INSERT INTO u VALUES (?)", [(c,) for c in codes])

    def top100_ranked(size_path: str):
        """方向：size(-1) 取小 → RANK ASC；amihud(+1) 取大 → RANK DESC。
        缺一因子的股票按「可得因子 rank 均值」处理（与 build_composite NaN-skip 一致）。"""
        return con.execute(f"""
            WITH s AS (SELECT date, code, value FROM read_parquet('{size_path}')
                       WHERE code IN (SELECT code FROM u)),
                 a AS (SELECT date, code, value FROM read_parquet('{AMIHUD}')
                       WHERE code IN (SELECT code FROM u)),
            rs AS (SELECT date, code,
                          RANK() OVER (PARTITION BY date ORDER BY value ASC) AS rn
                   FROM s),
            ra AS (SELECT date, code,
                          RANK() OVER (PARTITION BY date ORDER BY value DESC) AS an
                   FROM a),
            sc AS (
                SELECT COALESCE(rs.date, ra.date) AS date,
                       COALESCE(rs.code, ra.code) AS code,
                       (COALESCE(rs.rn, ra.an) + COALESCE(ra.an, rs.rn)) / 2.0 AS score
                FROM rs FULL JOIN ra ON rs.date = ra.date AND rs.code = ra.code
            )
            SELECT date, code,
                   ROW_NUMBER() OVER (PARTITION BY date ORDER BY score) AS rn
            FROM sc
            WHERE score IS NOT NULL
            QUALIFY rn <= 100
        """).df()

    o = top100_ranked(OLD_SIZE)
    n = top100_ranked(NEW_SIZE)
    j = o.merge(n, on=["date", "rn"], suffixes=("_old", "_new"))
    j["same_pos"] = j["code_old"] == j["code_new"]

    # 名单集合重叠率（真正关心的口径：持仓标的换了几只）
    ov = (o.merge(n, on=["date", "code"])
            .groupby("date").size().rename("n_overlap").reset_index())
    ov["overlap_pct"] = ov["n_overlap"] / 100.0
    ov = ov.sort_values("date")
    dates = sorted(ov["date"].unique())

    print()
    print("=" * 74)
    print("① 生产 top100 名单重叠率（每日交集 / 100）")
    print("=" * 74)
    print(f"  交易日数: {len(dates)}（{str(dates[0])[:10]} ~ {str(dates[-1])[:10]}）")
    print(f"  全历史平均重叠: {ov['overlap_pct'].mean()*100:.2f}%  "
          f"（最低 {ov['overlap_pct'].min()*100:.0f}%）")
    print(f"  位次完全一致（同一 rank 同一标的）: {j['same_pos'].mean()*100:.2f}%")

    print()
    print("② 分段重叠率")
    for label, k in [("最近 250 交易日", 250), ("最近 60 交易日", 60),
                     ("最近 20 交易日", 20), ("最新 1 日", 1)]:
        sub = ov[ov["date"].isin(dates[-k:])]
        print(f"  {label:14s} {sub['overlap_pct'].mean()*100:6.2f}%  "
              f"（{sub['date'].nunique()} 日）")

    print()
    print("③ 按年重叠率（越大 = 口径切换影响越小）")
    ov["year"] = ov["date"].apply(lambda d: d.year)
    for y, g in ov.groupby("year"):
        print(f"  {y}  {g['overlap_pct'].mean()*100:6.2f}%  "
              f"（{len(g)} 日，最低 {g['overlap_pct'].min()*100:.0f}%）")

    last = dates[-1]
    lo = set(o[o["date"] == last]["code"])
    ln = set(n[n["date"] == last]["code"])
    print()
    print(f"④ 最新交易日 {str(last)[:10]} 名单差异")
    print(f"  交集 {len(lo & ln)} 只 | 掉出 {len(lo - ln)} 只 | 新进 {len(ln - lo)} 只")
    print(f"  掉出: {sorted(lo - ln)}")
    print(f"  新进: {sorted(ln - lo)}")
    con.close()


if __name__ == "__main__":
    main()
