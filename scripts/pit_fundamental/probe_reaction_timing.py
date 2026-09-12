# -*- coding: utf-8 -*-
"""市场反应时点事件研究 v2：带基线对照的 spike 率曲线。

对每个披露事件 (code, pub=InfoPublDate)，在 t0（pub 当日或其后首个有 bar 的交易日）
前后 [-5, +5] 个交易日（个股自身序列，停牌跳过）逐日测 spike：
  spike = |个股收益 - 指数收益(000852)| >= 2%  或  量比(vol/vol_ma20) >= 2.0
输出逐 offset 的 spike 率曲线 —— 远端 offset 自带基线，近端超额才是披露效应。

first_reaction = 最早 spike 的 offset 桶：
  <=-2 更早 / -1 t-1 / 0 t0(开盘跳空 vs 盘中分解) / +1 t+1 / >=+2 更晚 / none
判读：信息真实可得时点 = 市场首次反应时点（有反应事件才可观测）。
危险桶 = t+1 及更晚（信息在 t0 收盘后才公开的代理）。
分钟子样本：观察日及其前一交易日扫分钟湖，首个>=0.5%跳变的分钟桶 + 09:31 跳空分布。
只读：主库 READ_ONLY ATTACH + 分钟湖 parquet。
"""
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

import duckdb
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
MAIN = str(ROOT / "data/quant.duckdb")
FQ_INC = str(ROOT / "data/lake/clean/fundamental/finance_q/income/part-*.parquet")
FQ_CF = str(ROOT / "data/lake/clean/fundamental/finance_q/cashflow/part-*.parquet")
MIN_GLOB = str(ROOT / "data/lake/clean/kline_1min/year=20*/part-*.parquet")
OUT = ROOT / "reports/pit_fundamental"
OUT.mkdir(parents=True, exist_ok=True)
IDX = "000852"
OFFSETS = list(range(-5, 6))


def attach_read_only(retry: int = 12) -> duckdb:
    for i in range(retry):
        try:
            con = duckdb.connect()
            con.execute(f"ATTACH '{MAIN}' AS m (READ_ONLY)")
            return con
        except Exception as e:
            if i == retry - 1:
                raise
            print(f"  主库忙，等锁重试 {i + 1}/{retry}: {type(e).__name__}")
            time.sleep(5)
    raise RuntimeError("unreachable")


def main() -> None:
    con = attach_read_only()
    kd_cols = {r[0] for r in con.execute("DESCRIBE m.kline_daily").fetchall()}
    dcol = "date" if "date" in kd_cols else "day"

    ev = con.execute(
        f"""
        SELECT code, CAST(InfoPublDate AS DATE) AS pub
        FROM (
            SELECT code, InfoPublDate FROM read_parquet('{FQ_INC}', union_by_name=true)
            UNION ALL
            SELECT code, InfoPublDate FROM read_parquet('{FQ_CF}', union_by_name=true)
        )
        WHERE InfoPublDate IS NOT NULL
          AND CAST(InfoPublDate AS DATE) BETWEEN '2024-01-01' AND '2026-09-10'
        GROUP BY code, CAST(InfoPublDate AS DATE)
        """
    ).fetchdf()
    print(f"事件数: {len(ev)}")

    # ---------- 长格式：事件 x offset 的 (ret, gap, vr, ir) ----------
    q = f"""
    WITH k AS (
        SELECT code, CAST({dcol} AS DATE) AS d, open, close, vol
        FROM m.kline_daily
        WHERE CAST({dcol} AS DATE) BETWEEN '2023-09-01' AND '2026-09-12'
    ),
    kd AS (
        SELECT code, d, open, close, vol,
               close / NULLIF(LAG(close) OVER (PARTITION BY code ORDER BY d), 0) - 1 AS ret,
               open / NULLIF(LAG(close) OVER (PARTITION BY code ORDER BY d), 0) - 1 AS gap,
               vol / NULLIF(AVG(vol) OVER (PARTITION BY code ORDER BY d
                       ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING), 0) AS vr,
               ROW_NUMBER() OVER (PARTITION BY code ORDER BY d) AS seq
        FROM k
    ),
    idx AS (
        SELECT CAST({dcol} AS DATE) AS d,
               close / NULLIF(LAG(close) OVER (ORDER BY d), 0) - 1 AS ir,
               ROW_NUMBER() OVER (ORDER BY d) AS ird
        FROM m.index_kline
        WHERE code LIKE '{IDX}%' AND CAST({dcol} AS DATE) BETWEEN '2023-09-01' AND '2026-09-12'
    ),
    offs(o) AS (VALUES {",".join(f"({o})" for o in OFFSETS)}),
    evk AS (
        SELECT e.code, e.pub, kd.d AS t0, kd.seq AS t0_seq, i.ird AS t0_ird
        FROM ev e
        JOIN kd ON kd.code = e.code AND kd.d >= e.pub
        JOIN idx i ON i.d = kd.d
        QUALIFY ROW_NUMBER() OVER (PARTITION BY e.code, e.pub ORDER BY kd.d) = 1
    )
    SELECT v.code, v.pub, v.t0, offs.o,
           k.ret, k.gap, k.vr, i.ir
    FROM evk v
    CROSS JOIN offs
    JOIN kd k ON k.code = v.code AND k.seq = v.t0_seq + offs.o
    JOIN idx i ON i.ird = v.t0_ird + offs.o
    """
    long = con.execute(q).fetchdf()
    long["pub"] = pd.to_datetime(long["pub"]).dt.date
    print(f"长表行: {len(long)} (期望 ~{len(ev) * len(OFFSETS)})")
    long["ar"] = long["ret"] - long["ir"]
    long["spike"] = (long["ar"].abs() >= 0.02) | (long["vr"] >= 2.0)

    # spike 率曲线（含基线）
    curve = long.groupby("o")["spike"].mean().round(4).to_dict()
    print("spike 率曲线:", curve)

    # first_reaction 桶
    first = (
        long[long["spike"]]
        .sort_values("o")
        .groupby(["code", "pub"])["o"]
        .first()
    )
    ev2 = ev.copy()
    ev2["pub"] = pd.to_datetime(ev2["pub"]).dt.date
    ev2 = ev2.merge(first.rename("first_o"), left_on=["code", "pub"],
                    right_index=True, how="left")

    def bucket(o):
        if o != o:
            return "none"
        if o <= -2:
            return "earlier_le_-2"
        if o == -1:
            return "t-1"
        if o == 0:
            return "t0"
        if o == 1:
            return "t+1"
        return "later_ge_+2"
    ev2["first_bucket"] = [bucket(o) for o in ev2["first_o"]]
    n = len(ev2)
    dist = {k: round(v / n, 4) for k, v in sorted(Counter(ev2["first_bucket"]).items())}
    print("first_reaction 分布:", dist)
    by_year = {}
    for y in (2024, 2025, 2026):
        sub = ev2[ev2["pub"].map(lambda x: x.year == y)]
        if len(sub):
            by_year[y] = {k: round(v / len(sub), 4) for k, v in sorted(Counter(sub["first_bucket"]).items())}

    # t0 反应内部分解（gap vs intraday 需要分+盘中收益，从 kd 单独取 t0 行）
    t0row = con.execute(
        f"""
        WITH k AS (
            SELECT code, CAST({dcol} AS DATE) AS d, open, close,
                   open / NULLIF(LAG(close) OVER (PARTITION BY code ORDER BY d), 0) - 1 AS gap
            FROM m.kline_daily
            WHERE CAST({dcol} AS DATE) BETWEEN '2024-01-01' AND '2026-09-12'
        ),
        evk AS (
            SELECT e.code, e.pub, k.d AS t0
            FROM ev e JOIN k ON k.code = e.code AND k.d >= e.pub
            QUALIFY ROW_NUMBER() OVER (PARTITION BY e.code, e.pub ORDER BY k.d) = 1
        )
        SELECT v.code, v.pub, k.d AS t0, k.gap, k.close / NULLIF(k.open, 0) - 1 AS intraday
        FROM evk v JOIN k ON k.code = v.code AND k.d = v.t0
        """
    ).fetchdf()
    t0row["mode"] = (t0row["gap"].abs() >= t0row["intraday"].abs()).map({True: "gap_open", False: "intraday"})
    mode_dist = t0row["mode"].value_counts(normalize=True).round(4).to_dict()
    print("t0 反应分解(gap vs 盘中):", mode_dist)

    # pub 落在非交易日的占比（天然安全桶）
    trading = {r[0] for r in con.execute(
        "SELECT DISTINCT CAST(trade_date AS DATE) FROM m.trade_calendar "
        "WHERE CAST(trade_date AS DATE) BETWEEN '2024-01-01' AND '2026-09-12'"
    ).fetchall()}
    pub_nontrade = round(float((~ev2["pub"].map(lambda x: x in trading)).mean()), 4)

    # ---------- 分钟子样本 ----------
    minute_stats = None
    try:
        peak_days = {r[0] for r in con.execute(
            """
            WITH cal AS (SELECT DISTINCT CAST(trade_date AS DATE) d FROM m.trade_calendar
                         WHERE CAST(trade_date AS DATE) BETWEEN '2025-01-01' AND '2026-09-12'),
            ranked AS (SELECT d, ROW_NUMBER() OVER (PARTITION BY year(d), month(d) ORDER BY d DESC) rn FROM cal)
            SELECT d FROM ranked WHERE rn <= 3
            """
        ).fetchall()}
        random.seed(7)
        # 观察对象：first spike 在 t0 或 t+1 的事件
        sel = ev2[(ev2.first_bucket.isin(["t0", "t+1"])) & ev2["pub"].map(lambda x: x.year >= 2025)]
        # 观察日 = t0（first=t0 的事件）或 t0 的下一交易日（first=t+1 的事件）
        nxt = dict(con.execute(
            "WITH cal AS (SELECT DISTINCT CAST(trade_date AS DATE) d FROM m.trade_calendar "
            "WHERE CAST(trade_date AS DATE) BETWEEN '2024-12-01' AND '2026-09-12') "
            "SELECT a.d, min(b.d) FROM cal a JOIN cal b ON b.d > a.d GROUP BY a.d"
        ).fetchall())
        prev = {v: k for k, v in nxt.items()}
        # t0 需要 join 回：t0row 有 code/pub -> t0
        t0row = t0row.copy()
        t0row["pub"] = pd.to_datetime(t0row["pub"]).dt.date
        t0row["t0"] = pd.to_datetime(t0row["t0"]).dt.date
        t0map = dict(zip(zip(t0row["code"], t0row["pub"]), t0row["t0"]))
        obs = []
        for r in sel.itertuples():
            t0 = t0map.get((r.code, r.pub))
            if t0 is None:
                continue
            d = t0 if r.first_bucket == "t0" else nxt.get(t0, t0)
            obs.append((r.code, d, r.first_bucket))
        if len(obs) > 1400:
            random.shuffle(obs)
            obs = obs[:1400]
        days = sorted({d for _, d, _ in obs})
        days_ext = sorted(set(days) | {prev[d] for d in days if d in prev})
        codes = sorted({c for c, _, _ in obs})
        print(f"分钟子样本: {len(obs)} 事件, {len(codes)} 只, 观察日 {len(days)}(含前日 {len(days_ext)})")
        day_list = ",".join(f"'{d.isoformat()}'" for d in days_ext)
        code_list = ",".join(f"'{c}'" for c in codes)
        mq = f"""
        WITH m AS (
            SELECT code, datetime, close,
                   close / LAG(close) OVER (PARTITION BY code ORDER BY datetime) - 1 AS r1
            FROM read_parquet('{MIN_GLOB}', union_by_name=true, hive_partitioning=false)
            WHERE code IN ({code_list})
        )
        SELECT code, CAST(datetime AS DATE) AS d,
               min(CASE WHEN abs(r1) >= 0.005 THEN strftime(datetime, '%H:%M') END) AS first_jump,
               max(CASE WHEN strftime(datetime, '%H:%M') = '09:31' THEN abs(r1) END) AS open_gap
        FROM m
        WHERE CAST(datetime AS DATE) IN ({day_list})
        GROUP BY code, CAST(datetime AS DATE)
        """
        mdf = con.execute(mq).fetchdf()
        mdf["d"] = pd.to_datetime(mdf["d"]).dt.date
        obs_df = pd.DataFrame(obs, columns=["code", "d", "tag"])
        mrg = obs_df.merge(mdf, on=["code", "d"], how="left")

        def mbucket(m):
            if m is None or m != m:
                return "no_jump"
            h = int(m[:2]) * 60 + int(m[3:5])
            if h <= 571:
                return "09:31_open"
            if h <= 600:
                return "09:32-10:00"
            if h <= 690:
                return "10:00-11:30"
            if h <= 870:
                return "13:00-14:30"
            return "14:30-15:00"
        mrg["bucket"] = [mbucket(m) for m in mrg["first_jump"]]
        total = len(mrg)
        by_tag = {}
        for tag in ("t0", "t+1"):
            sub = mrg[mrg.tag == tag]
            if len(sub):
                by_tag[tag] = {
                    "n": len(sub),
                    "first_jump_bucket": {k: round(v / len(sub), 4) for k, v in sorted(Counter(sub["bucket"]).items())},
                    "open_gap_ge_1pct": round(float((sub["open_gap"] >= 0.01).mean()), 4),
                    "open_gap_ge_2pct": round(float((sub["open_gap"] >= 0.02).mean()), 4),
                }
        minute_stats = {"n_events": total, "by_tag": by_tag}
        print("分钟首跳分布:", json.dumps(by_tag, ensure_ascii=False))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("分钟部分失败(不影响日线结论):", type(e).__name__, str(e)[:200])

    result = {
        "n_events": n,
        "pub_non_trading_day_share": pub_nontrade,
        "spike_rate_curve_by_offset": {str(k): v for k, v in sorted(curve.items())},
        "first_reaction_dist": dist,
        "by_year": by_year,
        "t0_mode_dist": mode_dist,
        "minute_subsample": minute_stats,
    }
    (OUT / "reaction_timing_probe.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print("=== 汇总 ===")
    print(json.dumps({k: v for k, v in result.items() if k != "by_year"}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
