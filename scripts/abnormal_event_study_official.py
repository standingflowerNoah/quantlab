"""严重异动事件研究 v2：交易所官方异常波动口径。

用户定义：严重异动 = 触发交易所《交易规则》异常波动标准、需发布
《股票交易异常波动公告》的股票。官方标准：

- 沪深主板：连续 3 个交易日内收盘价涨跌幅偏离值累计 ±20%（ST ±12%）
- 创业板/科创板：连续 3 个交易日内收盘价涨跌幅偏离值累计 ±30%
- 偏离值 = 单只股票涨跌幅 − 对应指数涨跌幅

实现口径（与官方的近似声明）
------------------------------
- 基准指数（官方对应综指不在库，用成分指数近似）：
  SH 主板(60x)→上证指数 000001.SH；SZ 主板(00x)→深证成指 399001.SZ；
  创业板(30x)→创业板指 399006.SZ；科创板(68x)→科创50 000688.SH
- 触发阈值带 0.5pct 容差（19.5%/29.5%/11.7%）对冲成分指数 vs 综指的基准偏差
- 去重：触发后 3 个交易日冷却（同一连板周期每 3 日最多 1 个事件，
  近似官方"披露后重新起算"）
- 附加过滤（声明）：上市≥60 交易日（新股前 5 日无涨跌幅限制=规则豁免区，
  60 日同时排除次新）；北交所剔除（库内无北证综指+流动性）
- 资金流/换手/量比等特征 join 自 daily_panel 与 moneyflow 湖表
- 交叉验证：自算事件 vs 龙虎榜官方"连续三个交易日"条目的召回率
- 严重异常波动子集（sev_abn）：滚动 10 日累计偏离 ≥+100% 或 ≤−50%
  （交易所"严重异常波动"停牌核查级的量化近似）

输出：reports/_tmp/abnormal_events_official.parquet
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "quant.duckdb"
MF_DIR = ROOT / "data" / "lake" / "clean" / "moneyflow"
TMP = ROOT / "reports" / "_tmp"

# 触发阈值（含 0.5pct 容差）
THR_MAIN = 0.195     # 主板 20%
THR_CM = 0.295       # 创业板/科创板 30%
THR_ST = 0.117       # ST 12%
COOLDOWN = 3         # 触发后冷却交易日数
LIST_DAYS_MIN = 60
SEV_UP = 1.00        # 严重异常波动：10日累计偏离 +100%
SEV_DN = -0.50       # -50%


def bench_map(code: str, board: str) -> str | None:
    if board == "BJ":
        return None
    if board == "GEM":
        return "399006.SZ"
    if board == "STAR":
        return "000688.SH"
    # MAIN: 按代码前缀分市场
    if code.startswith("6"):
        return "000001.SH"
    return "399001.SZ"


def main() -> None:
    # ── 1. 指数收益 ──────────────────────────────────────────────
    con = duckdb.connect(str(DB), read_only=True)
    idx = con.execute("""
        select date, code, close,
               lag(close) over (partition by code order by date) as prev_close
        from index_kline
        where code in ('000001.SH','399001.SZ','399006.SZ','000688.SH')
    """).fetchdf()
    con.close()
    idx["date"] = pd.to_datetime(idx["date"])
    idx["bench_ret"] = idx["close"] / idx["prev_close"] - 1
    idx = idx.dropna(subset=["bench_ret"])[["date", "code", "bench_ret"]]
    bench_by_code = {c: g.set_index("date")["bench_ret"] for c, g in idx.groupby("code")}

    # ── 2. 日线面板 ──────────────────────────────────────────────
    dp = pd.read_parquet(TMP / "daily_panel.parquet")
    dp = dp[dp["board"] != "BJ"].copy()          # 北交所剔除（声明）
    dp = dp.dropna(subset=["ret"]).sort_values(["code", "date"]).reset_index(drop=True)

    # bench 映射
    pairs = {}
    for (c, b) in dp[["code", "board"]].drop_duplicates().itertuples(index=False):
        pairs[c] = bench_map(c, b)
    dp["bench"] = dp["code"].map(pairs)

    # bench ret 按 (bench, date) join
    idx_flat = idx.rename(columns={"code": "bench"})
    dp = dp.merge(idx_flat, on=["bench", "date"], how="left")
    dp["dev"] = dp["ret"] - dp["bench_ret"]      # 单日偏离值
    n_nobench = dp["bench_ret"].isna().sum()
    print(f"日线 {len(dp):,} 行；bench 缺失 {n_nobench:,} 行 ({n_nobench/len(dp):.1%})")

    # ── 3. 状态机：3 日滚动累计偏离触发 + 3 日冷却去重 ───────────
    thr_col = np.where(dp["board"].isin(["GEM", "STAR"]), THR_CM, THR_MAIN)
    thr_col = np.where(dp["is_st"].fillna(False).astype(bool), THR_ST, thr_col)
    dp["thr"] = thr_col

    events = []
    for code, g in dp.groupby("code", sort=False):
        devs = g["dev"].to_numpy()
        dates = g["date"].to_numpy()
        is_st_arr = g["is_st"].fillna(False).astype(bool).to_numpy()
        thr_arr = g["thr"].to_numpy()
        rows_g = g.index.to_numpy()
        last_ev = -99
        n = len(g)
        # 滚动3日累计偏离
        dev3 = np.full(n, np.nan)
        dev3[2:] = devs[2:] + devs[1:-1] + devs[:-2]
        # 滚动10日累计偏离（严重异动子集）
        dev10 = np.full(n, np.nan)
        csum = np.nancumsum(devs)
        for i in range(9, n):
            dev10[i] = csum[i] - (csum[i - 10] if i >= 10 else 0)
        for i in range(2, n):
            if i <= last_ev + COOLDOWN - 1:
                continue
            d3 = dev3[i]
            if np.isnan(d3):
                continue
            thr_i = thr_arr[i] if not is_st_arr[i] else THR_ST
            if abs(d3) >= thr_i:
                sev = False
                if not np.isnan(dev10[i]) and i >= 9:
                    sev = (dev10[i] >= SEV_UP) or (dev10[i] <= SEV_DN)
                events.append((rows_g[i], d3, dev10[i] if not np.isnan(dev10[i]) else np.nan, sev))
                last_ev = i
    ev_idx = pd.DataFrame(events, columns=["row_idx", "dev3", "dev10", "sev_abn"])
    ev = dp.loc[ev_idx["row_idx"]].copy().reset_index(drop=True)
    ev["dev3"] = ev_idx["dev3"].to_numpy()
    ev["dev10"] = ev_idx["dev10"].to_numpy()
    ev["sev_abn"] = ev_idx["sev_abn"].to_numpy()
    ev["direction"] = np.where(ev["dev3"] > 0, "UP", "DOWN")
    # 上市≥60 日过滤
    ev = ev[ev["bar_no"] >= LIST_DAYS_MIN].copy()

    # ── 4. 资金流 join ───────────────────────────────────────────
    files = sorted(MF_DIR.glob("part-*.parquet"))
    if files:
        c2 = duckdb.connect()
        fs = ", ".join(f"'{f.as_posix()}'" for f in files)
        mf = c2.execute(f"""
            select date, code, main_net, super_net, large_net, mid_net, small_net, net_mf_amount
            from read_parquet([{fs}], hive_partitioning=false)
        """).fetchdf()
        c2.close()
        mf["date"] = pd.to_datetime(mf["date"])
        ev = ev.merge(mf, on=["date", "code"], how="left")
        for c in ["main_net", "super_net", "large_net", "mid_net", "small_net", "net_mf_amount"]:
            ev[f"{c}_pct"] = ev[c] * 1e4 / ev["amount"]
    ev.to_parquet(TMP / "abnormal_events_official.parquet", index=False)

    # ── 5. 汇总与交叉验证 ────────────────────────────────────────
    print(f"\n官方口径异动事件: {len(ev):,} 个 / {ev['code'].nunique():,} 只 / "
          f"{ev['date'].min().date()} ~ {ev['date'].max().date()}")
    print("\n=== 方向 × 板块 ===")
    print(ev.pivot_table(index="direction", columns="board", values="code", aggfunc="count").to_string())
    print("\n=== ST 占比 ===")
    print(ev["is_st"].fillna(False).astype(bool).value_counts().to_string())
    print(f"\n严重异常波动子集（10日累计偏离 ≥+100% 或 ≤-50%）: {ev['sev_abn'].sum():,}")
    print("\n=== 分年 ===")
    ev["year"] = ev["date"].dt.year
    print(ev.pivot_table(index="year", columns="direction", values="code", aggfunc="count").to_string())
    if "main_net_pct" in ev.columns:
        print(f"\n资金流 join 覆盖率: {ev['main_net'].notna().mean():.1%}")

    # 龙虎榜官方条目召回验证
    con = duckdb.connect(str(DB), read_only=True)
    dt = con.execute("""
        select distinct date, code from dragon_tiger
        where (reason like '%连续三个交易日%' and reason not like '%可转债%'
               and reason not like '%封闭式基金%')
    """).fetchdf()
    con.close()
    dt["date"] = pd.to_datetime(dt["date"])
    # ±1 日窗口匹配（榜单披露日=触发日或次日）
    ev_keys = set(zip(ev["code"], ev["date"]))
    dt_hit = 0
    for c, d in zip(dt["code"], dt["date"]):
        for off in (-1, 0, 1):
            if (c, d + pd.Timedelta(days=off)) in ev_keys:
                dt_hit += 1
                break
    print(f"\n=== 交叉验证：龙虎榜官方异动条目 {len(dt):,} 榜次，"
          f"被自算事件（±1日）捕获 {dt_hit:,} = {dt_hit/len(dt):.1%} ===")


if __name__ == "__main__":
    main()
