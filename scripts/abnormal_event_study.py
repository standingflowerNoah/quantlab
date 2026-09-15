"""严重异动事件研究：数据准备与事件样本构造。

数据口径（2026-09-13 定稿）
--------------------------
- 日线: 主库 kline_daily 2022-01-04~（date/code/ohlcv/amount/adj_factor）
- 收益: 复权收益 ret = close_T*adj_T / (close_{T-1}*adj_{T-1}) - 1
        （除权日连续，已抽样验证；fsdb pre_close 不可信的老坑绕开）
- 换手率: turnover = vol / float_shares（share_capital_daily，与龙虎榜官方
          turnover_pct 比值中位 1.0000 校准通过）
- 板块: instruments.board（主板/创业板/科创板/北交所）+ is_st 过滤
- 上市天数: code 首次出现于 kline_daily 的行号（≥60 交易日才入池，防次新）

严重异动事件定义（先定后跑，对标交易所披露口径）
------------------------------------------------
事件日 T 须同时满足：
  A. 价格极端: ret_T >= +7% 或 <= -7%（全板块统一绝对阈值）
  B. 放量确认: turnover_T >= 15%
  C. 流动性:   amount_T >= 1e8（亿元成交额，保证可执行性）
  D. 非次新:   上市 >= 60 交易日; 非 ST
方向分层: UP(ret>=7%) / DOWN(ret<=-7%)
巨震子集: |ret|<7% 且 振幅>=15% 且 turnover>=15%（价格未极端但日内极端）

前瞻收益（两口径）
------------------
- fwd_N = close_{T+N}/close_T - 1              （事件研究理论口径）
- exec_N = close_{T+N}/open_{T+1} - 1          （T+1 开盘可执行口径）
- 涨跌停不可成交剔除：T+1 开盘一字涨停（open==high==low 且涨幅触板）→ 买不进，
  标记 entry_blocked=1，可执行口径置 NaN
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "quant.duckdb"
MF_DIR = ROOT / "data" / "lake" / "clean" / "moneyflow"
OUT = ROOT / "reports" / "_tmp"

# 事件参数（全局唯一口径）
RET_TH = 0.07          # 单日涨跌幅阈值
TO_TH = 0.15           # 换手率阈值
AMT_TH = 1e8           # 成交额阈值（元）
LIST_DAYS_MIN = 60     # 最低上市交易日数
AMP_TH = 0.15          # 巨震振幅阈值
LIMIT_MAIN = 0.095     # 主板涨跌停近似（一字判定用）
LIMIT_20CM = 0.195     # 创业/科创
LIMIT_BJ = 0.295       # 北交所


def load_daily(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """日线+换手率+板块，含窗口特征。"""
    q = f"""
    with px as (
        select k.date, k.code, k.open, k.high, k.low, k.close, k.vol, k.amount,
               k.adj_factor,
               lag(k.close)  over w as prev_close,
               lag(k.adj_factor) over w as prev_adj,
               avg(k.vol) over (partition by k.code order by k.date
                                rows between 21 preceding and 1 preceding) as vol_ma20,
               row_number() over (partition by k.code order by k.date) as bar_no,
               count(*) over (partition by k.code) as bars_total
        from kline_daily k
        where k.date >= '2021-10-01' and k.amount > 0 and k.vol > 0
        window w as (partition by k.code order by k.date)
    )
    select p.date, p.code, p.open, p.high, p.low, p.close, p.vol, p.amount,
           p.adj_factor, p.bar_no, p.bars_total,
           case when p.prev_close is null or p.prev_adj is null then null
                else (p.close*p.adj_factor)/(p.prev_close*p.prev_adj) - 1 end as ret,
           case when p.prev_close is null then null
                else (p.high - p.low)/p.prev_close end as amplitude,
           case when p.vol_ma20 is null or p.vol_ma20 = 0 then null
                else p.vol / p.vol_ma20 end as vol_ratio,
           s.float_shares,
           case when s.float_shares > 0 then p.vol / s.float_shares end as turnover,
           i.board, i.is_st
    from px p
    left join share_capital_daily s on s.code = p.code and s.date = p.date
    left join (select code, board, is_st from instruments) i on i.code = p.code
    """
    return con.execute(q).fetchdf()


def add_forward(d: pd.DataFrame, n_list=(1, 5, 10, 20)) -> pd.DataFrame:
    """前瞻收益：理论口径（T close 基准）与可执行口径（T+1 open 基准）。

    T+1 一字涨停开盘 = open==high==low 且 (open/prev_close-1) >= 板块限价-容差
    → entry_blocked，exec_* 置 NaN。
    """
    d = d.sort_values(["code", "date"]).reset_index(drop=True)
    g = d.groupby("code", sort=False)
    nxt_open = g["open"].shift(-1)
    nxt1_close = g["close"].shift(-1)
    nxt1_high = g["high"].shift(-1)
    nxt1_low = g["low"].shift(-1)
    nxt1_open = nxt_open  # alias
    # T+1 开盘相对 T 收盘的跳空（用原始价，当日无除权时等于复权跳空；
    # 除权日由 adj 校正——直接用 open*adj 比较）
    t_close_adj = d["close"] * d["adj_factor"]
    nxt1_open_adj = nxt1_open * g["adj_factor"].shift(-1)
    gap = nxt1_open_adj / t_close_adj - 1
    # 一字板判定：T+1 open==high==low（全天无波动）
    one_word = (nxt1_open == nxt1_high) & (nxt1_open == nxt1_low)
    limit = np.where(d["board"].isin(["GEM", "STAR"]), LIMIT_20CM,
             np.where(d["board"] == "BJ", LIMIT_BJ, LIMIT_MAIN))  # MAIN/NaN→10% 保守
    # NaN board 按 10% 处理（保守）
    limit = np.where(pd.isna(limit), LIMIT_MAIN, limit)
    entry_blocked = one_word & (gap >= limit * 0.9)
    d["gap_open"] = gap
    d["entry_blocked"] = entry_blocked.fillna(False)

    close_adj = d["close"] * d["adj_factor"]
    for n in n_list:
        f = g["close"].shift(-1 - (n - 1)) if n > 1 else nxt1_close
        f_adj = f * g["adj_factor"].shift(-1 - (n - 1))
        d[f"fwd_{n}"] = f_adj / close_adj - 1
        # 可执行：T+1 open 买入 → T+N close 卖出
        base = nxt1_open_adj
        d[f"exec_{n}"] = f_adj / base - 1
    # entry_blocked 的 exec_* 置 NaN
    for n in n_list:
        d.loc[d["entry_blocked"], f"exec_{n}"] = np.nan
    return d


def build_events(d: pd.DataFrame) -> pd.DataFrame:
    """严重异动事件表（含方向分层与巨震子集）。"""
    m = (
        d["ret"].abs().ge(RET_TH)
        & d["turnover"].ge(TO_TH)
        & d["amount"].ge(AMT_TH)
        & d["bar_no"].ge(LIST_DAYS_MIN)
        & (~d["is_st"].fillna(False).astype(bool))
    )
    ev = d[m].copy()
    ev["direction"] = np.where(ev["ret"] >= RET_TH, "UP", "DOWN")
    # 巨震子集
    m2 = (
        d["ret"].abs().lt(RET_TH)
        & d["amplitude"].ge(AMP_TH)
        & d["turnover"].ge(TO_TH)
        & d["amount"].ge(AMT_TH)
        & d["bar_no"].ge(LIST_DAYS_MIN)
        & (~d["is_st"].fillna(False).astype(bool))
    )
    sw = d[m2].copy()
    sw["direction"] = "SWING"
    return pd.concat([ev, sw], ignore_index=True)


def attach_moneyflow(ev: pd.DataFrame) -> pd.DataFrame:
    """join 资金流（万元→占比，分母=当日成交额元）。"""
    if not MF_DIR.exists():
        return ev
    files = sorted(MF_DIR.glob("part-*.parquet"))
    if not files:
        return ev
    con = duckdb.connect()
    files_sql = ", ".join(f"'{f.as_posix()}'" for f in files)
    mf = con.execute(f"""
        select date, code, main_net, super_net, large_net, mid_net, small_net, net_mf_amount
        from read_parquet([{files_sql}], hive_partitioning=false)
    """).fetchdf()
    con.close()
    mf["date"] = pd.to_datetime(mf["date"])
    ev = ev.merge(mf, on=["date", "code"], how="left")
    # 占比：万元×1e4 / 成交额元
    for c in ["main_net", "super_net", "large_net", "mid_net", "small_net", "net_mf_amount"]:
        ev[f"{c}_pct"] = ev[c] * 1e4 / ev["amount"]
    return ev


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB), read_only=True)
    d = load_daily(con)
    con.close()
    d["date"] = pd.to_datetime(d["date"])
    d = add_forward(d)
    d.to_parquet(OUT / "daily_panel.parquet", index=False)  # 下游分析缓存
    ev = build_events(d)
    ev = attach_moneyflow(ev)
    ev.to_parquet(OUT / "abnormal_events.parquet", index=False)

    print(f"日线 {len(d):,} 行 / {d['code'].nunique():,} 只")
    print(f"严重异动事件 {len(ev):,} 个 / {ev['code'].nunique():,} 只 / "
          f"{ev['date'].min().date()} ~ {ev['date'].max().date()}")
    print()
    print("=== 方向分布 ===")
    print(ev.groupby("direction").agg(
        n=("code", "size"), median_ret=("ret", "median"),
        median_to=("turnover", "median"), median_amt_yi=("amount", "median")).to_string())
    print()
    print("=== 分年事件数 ===")
    ev["year"] = ev["date"].dt.year
    print(ev.pivot_table(index="year", columns="direction", values="code", aggfunc="count").to_string())
    print()
    print(f"事件表 → {OUT / 'abnormal_events.parquet'}")
    if "main_net" in ev.columns:
        print(f"资金流 join 覆盖率: main_net 非空 {ev['main_net'].notna().mean():.1%}")
    else:
        print("资金流文件不存在，跳过 join（回补后重跑）")


if __name__ == "__main__":
    main()
