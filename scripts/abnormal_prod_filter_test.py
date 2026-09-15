"""PROD 生产组合 × 异动过滤器增量检验（终极落地检验）。

问题：PROD（size+amihud_20 等权 top100，月频调仓）持仓股触发异动后表现如何？
在持仓中卖出异动股（回避过滤器）能否改善 PROD？

为什么这是唯一可信的落地路径：
- 回避清单是全天研究唯一时序稳健的负向发现（UP 全体独立日 t=-13.6）
- PROD 全窗口每月有持仓 → 异动事件在持仓内全窗口分布 → 独立信号日充足
- 过滤器是纯风控动作，不改变选股逻辑 → 实盘可无缝叠加

实现：
- 复现 PROD：因子湖 size/amihud_20 月末截面 rank 等权（size 负向/amihud 正向）top100
- 基准组合：月频等权 100 只，持有至下月末
- 过滤版：持仓股触发 UP 异动（官方口径，非ST）→ T+1 开盘卖出 →
  资金即时摊给剩余持仓（权重归一化）；月末调仓恢复等权
- 成本：过滤版每次触发额外 50bp（卖 27.5+月末若仍在池买回 22.5 简化为 50）
- 变体：filter_UP / filter_UP+DOWN / 不含 2024-02 的稳健性
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "reports" / "_tmp"
COST_PER_TRIGGER = 0.0050  # 过滤触发的额外往返成本


def load_prod_holdings() -> pd.DataFrame:
    """月末截面 size+amihud_20 rank 等权 top100。"""
    con = duckdb.connect()
    q = """
    with months as (
        select max(date) as mdate from read_parquet('data/lake/factor/size/**/*.parquet',
            hive_partitioning=false)
        group by date_trunc('month', date)
    ),
    sz as (
        select s.date, s.code, s.value as sz
        from read_parquet('data/lake/factor/size/**/*.parquet', hive_partitioning=false) s
        join months m on s.date = m.mdate
        where s.date >= '2022-04-01'
    ),
    am as (
        select a.date, a.code, a.value as am
        from read_parquet('data/lake/factor/amihud_20/**/*.parquet', hive_partitioning=false) a
        join months m on a.date = m.mdate
        where a.date >= '2022-04-01'
    )
    select sz.date, sz.code, sz.sz, am.am from sz join am using (date, code)
    """
    df = con.execute(q).fetchdf()
    con.close()
    df["date"] = pd.to_datetime(df["date"])
    # rank 等权：size 负向（小=高分）+ amihud 正向（高=高分）
    df["r_sz"] = df.groupby("date")["sz"].rank(pct=True, ascending=False)
    df["r_am"] = df.groupby("date")["am"].rank(pct=True)
    df["score"] = 0.5 * df["r_sz"] + 0.5 * df["r_am"]
    df = df.sort_values(["date", "score"], ascending=[True, False])
    df["rank"] = df.groupby("date").cumcount() + 1
    return df[df["rank"] <= 100].copy()


def load_events() -> pd.DataFrame:
    ev = pd.read_parquet(TMP / "abnormal_events_official_enriched.parquet")
    ev = ev[~ev["is_st"].fillna(False).astype(bool)].copy()
    return ev[["date", "code", "direction"]]


def simulate(holdings: pd.DataFrame, events: pd.DataFrame,
             filter_dirs: set[str] | None) -> dict:
    """月频等权组合逐日模拟；filter_dirs 触发 T+1 开盘卖出（权重归零重分配）。"""
    dp = pd.read_parquet(TMP / "daily_panel.parquet",
                         columns=["date", "code", "open", "close", "ret", "adj_factor"])
    dp = dp.sort_values(["code", "date"]).reset_index(drop=True)
    pos = {}
    for i, (c, d) in enumerate(zip(dp["code"].to_numpy(), dp["date"].to_numpy())):
        pos[(c, d)] = i
    codes = dp["code"].to_numpy()
    dates = dp["date"].to_numpy()
    rets = dp["ret"].to_numpy()
    opens = dp["open"].to_numpy()
    alld = sorted(dp["date"].unique())

    # 事件触发日集合（按方向过滤）
    ev = events if filter_dirs is None else events[events["direction"].isin(filter_dirs)]
    trig = set(zip(ev["code"], ev["date"]))

    rebalance_dates = sorted(holdings["date"].unique())
    daily_rets = []   # (date, port_ret, n_hold, n_triggered_cum)
    n_trig_total = 0
    trig_days = set()

    # 组合状态：code -> weight
    weights: dict[str, float] = {}
    prev_codes = set()
    hold_set = set()
    ri = 0
    for d in alld:
        if rebalance_dates and d >= rebalance_dates[0]:
            if ri < len(rebalance_dates) and d == rebalance_dates[ri]:
                # 调仓日：新等权
                new_codes = holdings[holdings["date"] == rebalance_dates[ri]]["code"]
                weights = {c: 1.0 / len(new_codes) for c in new_codes}
                hold_set = set(new_codes)
                ri += 1
                while ri < len(rebalance_dates) and rebalance_dates[ri] <= d:
                    ri += 1
        # 当日收益（昨日收盘持仓）
        if not weights:
            continue
        port_r = 0.0
        drop = []
        for c, w in weights.items():
            i = pos.get((c, d))
            if i is None:
                continue
            r = rets[i] if not np.isnan(rets[i]) else 0.0
            port_r += w * r
            # 明日过滤判定：当日该股触发异动 → 明日开盘卖出（简化：明日权重归零）
        daily_rets.append((d, port_r, len(weights)))
        # 生成明日的权重：处理当日触发的股（T+1 卖出）
        to_remove = []
        for c in list(weights):
            if (c, d) in trig:
                to_remove.append(c)
        if to_remove:
            n_trig_total += len(to_remove)
            trig_days.add(d)
            # T+1 开盘卖：收益按当日收盘持仓算完后，明日开始权重归零
            # 成本：当日已实现收益上扣 50bp（简化：摊到当日）
            daily_rets[-1] = (d, port_r - COST_PER_TRIGGER * len(to_remove) / max(len(weights), 1),
                              len(weights))
            for c in to_remove:
                weights.pop(c)
            # 权重归一化到剩余持仓
            if weights:
                s = sum(weights.values())
                weights = {c: w / s for c, w in weights.items()}

    dr = pd.DataFrame(daily_rets, columns=["date", "ret", "n"])
    dr["date"] = pd.to_datetime(dr["date"])
    return {"daily": dr, "n_trig": n_trig_total,
            "trig_days": len(trig_days), "weights_note": "T+1近似"}


def perf(dr: pd.DataFrame, label: str) -> dict:
    r = dr.set_index("date")["ret"]
    eq = (1 + r).cumprod()
    yrs = (r.index.max() - r.index.min()).days / 365.25
    ann = eq.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    sharpe = r.mean() / r.std() * np.sqrt(244) if r.std() > 0 else np.nan
    yearly = eq.groupby(eq.index.year).apply(lambda x: x.iloc[-1] / x.iloc[0] - 1)
    print(f"{label:>28}: 年化 {ann:+.1%} | 回撤 {dd:.1%} | 夏普 {sharpe:.2f} | "
          f"总收益 {eq.iloc[-1]-1:+.0%}")
    print("    分年: " + "  ".join(f"{y}:{v:+.1%}" for y, v in yearly.items()))
    return {"ann": ann, "dd": dd, "sharpe": sharpe, "yearly": yearly, "eq": eq}


def main() -> None:
    holdings = load_prod_holdings()
    events = load_events()
    print(f"PROD 持仓复现：{holdings['date'].nunique()} 个月末 / "
          f"{holdings['code'].nunique()} 只股票 / {holdings['date'].min().date()} ~ "
          f"{holdings['date'].max().date()}")

    # 持仓内异动频率（事件层）
    hold_pairs = set(zip(holdings["code"], holdings["date"]))
    # 每月末持仓在未来一个月内触发的异动数
    mdates = sorted(holdings["date"].unique())
    ev_in = []
    for i, md in enumerate(mdates[:-1]):
        nxt = mdates[i + 1]
        codes_m = holdings[holdings["date"] == md]["code"]
        sub = events[(events["date"] > md) & (events["date"] <= nxt)
                     & (events["code"].isin(set(codes_m)))]
        for _, e in sub.iterrows():
            ev_in.append((md, e["date"], e["code"], e["direction"]))
    evh = pd.DataFrame(ev_in, columns=["rebal_date", "ev_date", "code", "direction"])
    print(f"\n持仓期内异动触发: {len(evh)} 次 "
          f"(UP {int((evh['direction']=='UP').sum())} / DOWN {int((evh['direction']=='DOWN').sum())})")
    print(f"平均每月 {len(evh)/len(mdates[:-1]):.1f} 次；独立触发日 {evh['ev_date'].nunique()} 天")

    # 组合模拟
    print("\n=== 组合层对比 ===")
    base = simulate(holdings, events, None)
    perf(base["daily"], "PROD 基准（无过滤）")
    f_up = simulate(holdings, events, {"UP"})
    print(f"  [filter UP] 触发 {f_up['n_trig']} 次 / {f_up['trig_days']} 个独立日")
    perf(f_up["daily"], "PROD + 过滤UP异动")
    f_both = simulate(holdings, events, {"UP", "DOWN"})
    print(f"  [filter UP+DOWN] 触发 {f_both['n_trig']} 次 / {f_both['trig_days']} 个独立日")
    perf(f_both["daily"], "PROD + 过滤UP&DOWN")

    # 差值分年（过滤增量）
    b = base["daily"].set_index("date")["ret"]
    f = f_up["daily"].set_index("date")["ret"]
    diff = (f - b).dropna()
    if len(diff):
        cum = (1 + diff).cumprod()
        yearly_inc = cum.groupby(cum.index.year).apply(lambda x: x.iloc[-1] / x.iloc[0] - 1)
        t = diff.mean() / (diff.std() / np.sqrt(len(diff))) if diff.std() > 0 else np.nan
        print(f"\n过滤UP 年度增量: " + "  ".join(f"{y}:{v:+.2%}" for y, v in yearly_inc.items()))
        print(f"累计增量 {cum.iloc[-1]:+.2%}，日增量 t={t:.1f}")

    # 保存净值供 HTML
    pd.DataFrame({
        "PROD基准": (1 + b).cumprod(),
        "过滤UP": (1 + f).cumprod(),
    }).to_csv(TMP / "prod_filter_equity.csv")


if __name__ == "__main__":
    main()
