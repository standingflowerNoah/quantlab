"""用户策略完整回测：DOWN 3日急跌20%+ + 当日主力净流入占比创20日新高 + 净流入，
持有至主力大幅流出（≤-5%）卖出，最长 20 交易日。全仓等权、成本后、终审审计口径。
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "reports" / "_tmp"
EXIT_TH = -0.05
MAX_HOLD = 20
COST_BUY = 0.00225
COST_SELL = 0.00275


def main() -> None:
    # ── 信号 ──
    ev = pd.read_parquet(TMP / "abnormal_events_official_enriched.parquet")
    ev = ev[~ev["is_st"].fillna(False).astype(bool)].copy()
    ev = ev[~ev["entry_blocked"].fillna(False)]
    dp = pd.read_parquet(TMP / "panel_with_mf20.parquet")
    ev = ev.merge(dp[["date", "code", "is_20d_high_mf"]],
                  on=["date", "code"], how="left")
    # 危机态
    con = duckdb.connect(str(ROOT / "data" / "quant.duckdb"), read_only=True)
    idx = con.execute("select date, close from index_kline where code='880003.SH' order by date").fetchdf()
    con.close()
    idx["date"] = pd.to_datetime(idx["date"]); idx = idx.sort_values("date").reset_index(drop=True)
    idx["ret20"] = idx["close"] / idx["close"].shift(20) - 1
    ev = ev.merge(idx[["date", "ret20"]], on="date", how="left")

    sig = ev[(ev["direction"] == "DOWN")
             & (ev["is_20d_high_mf"] == True)   # noqa: E712
             & (ev["main_net_pct"] > 0)].copy()
    print(f"信号数: {len(sig)}（DOWN 异动 & 主力净流占比=20日最高 & 净流入）")
    print(f"  对照: DOWN 全体 {int((ev['direction']=='DOWN').sum())}；S1s 624")

    # ── 动态出场回测（复用逐日路径法）──
    pos = {}
    for i, (c, d) in enumerate(zip(dp["code"].to_numpy(), dp["date"].to_numpy())):
        pos[(c, d)] = i
    codes = dp["code"].to_numpy(); rets = dp["ret"].to_numpy()
    opens = dp["open"].to_numpy(); closes = dp["close"].to_numpy()
    mfp = dp["main_net_pct"].to_numpy(); dates_arr = dp["date"].to_numpy()

    records = []
    for _, e in sig.iterrows():
        i0 = pos.get((e["code"], e["date"]))
        if i0 is None or i0 + 1 >= len(dp) or codes[i0 + 1] != e["code"]:
            continue
        i1 = i0 + 1
        r0 = closes[i1] / opens[i1] - 1
        cum = 1 + r0
        exit_i, reason = None, None
        for k in range(i1 + 1, min(i1 + MAX_HOLD, len(dp))):
            if codes[k] != e["code"]:
                break
            rk = rets[k] if not np.isnan(rets[k]) else 0
            cum *= (1 + rk)
            if not np.isnan(mfp[k]) and mfp[k] <= EXIT_TH:
                exit_i, reason = k, "主力流出"
                break
        if exit_i is None:
            end = min(i1 + MAX_HOLD, len(dp) - 1)
            j = end
            while j > i1 and codes[j] != e["code"]:
                j -= 1
            exit_i = j
            reason = "20日兜底" if (exit_i - i1) >= MAX_HOLD - 1 else "尾部截断"
        records.append({
            "信号日": e["date"], "code": e["code"],
            "买入日": dates_arr[i1], "卖出日": dates_arr[exit_i],
            "hold": exit_i - i1 + 1, "ret_gross": cum - 1, "reason": reason,
        })

    tr = pd.DataFrame(records)
    if tr.empty:
        print("无交易"); return
    tr["ret_net"] = tr["ret_gross"] - COST_BUY - COST_SELL
    tr["信号日"] = pd.to_datetime(tr["信号日"])
    tr["买入日"] = pd.to_datetime(tr["买入日"]); tr["卖出日"] = pd.to_datetime(tr["卖出日"])

    # ── 组合层（日历等权逐日）──
    rows = []
    for t_i, r in tr.iterrows():
        i1 = pos[(r["code"], r["信号日"])] + 1
        rows.append((dates_arr[i1], t_i, r["ret_gross"] if False else closes[i1]/opens[i1]-1))
        # 用逐日复利重算
    # 重新逐日展开
    rows = []
    for t_i, r in tr.iterrows():
        i0 = pos[(r["code"], r["信号日"])]
        i1 = i0 + 1
        rows.append((dates_arr[i1], t_i, closes[i1]/opens[i1]-1, 0))
        for k in range(i1+1, min(i1+MAX_HOLD, len(dp))+1):
            if k >= len(dp) or codes[k] != r["code"]:
                break
            rows.append((dates_arr[k], t_i,
                         rets[k] if not np.isnan(rets[k]) else 0.0,
                         1 if dates_arr[k] == r["卖出日"] else 0))
    pos_df = pd.DataFrame(rows, columns=["date", "tid", "ret", "is_exit"])
    pos_df["date"] = pd.to_datetime(pos_df["date"])
    daily = pos_df.groupby("date").agg(ret=("ret", "mean"), n=("tid", "nunique")).reset_index()
    entries = pos_df.drop_duplicates("tid").groupby("date").size()
    exits = pos_df[pos_df["is_exit"] == 1].groupby("date").size()
    n_open = daily.set_index("date")["n"]
    adj = pd.Series(0.0, index=daily["date"])
    for d, cnt in entries.items():
        if d in adj.index: adj[d] += COST_BUY * cnt / n_open[d]
    for d, cnt in exits.items():
        if d in adj.index: adj[d] += COST_SELL * cnt / n_open[d]
    daily["net"] = daily["ret"] - adj.values
    eq = (1 + daily.set_index("date")["net"]).cumprod()
    yrs = (daily["date"].max() - daily["date"].min()).days / 365.25
    ann = eq.iloc[-1] ** (1/yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    sh = daily["net"].mean() / daily["net"].std() * np.sqrt(244) if daily["net"].std() > 0 else np.nan
    yearly = eq.groupby(eq.index.year).apply(lambda x: x.iloc[-1]/x.iloc[0]-1)

    print("\n===== 组合层（成本后）=====")
    print(f"  交易 {len(tr)} 笔 | 年化 {ann:+.1%} | 回撤 {dd:.1%} | 夏普 {sh:.2f}")
    print(f"  分年: " + "  ".join(f"{y}:{v:+.1%}" for y, v in yearly.items()))
    print(f"  单笔: win {(tr['ret_net']>0).mean():.0%} | 中位 {tr['ret_net'].median():+.1%} | "
          f"均值 {tr['ret_net'].mean():+.1%} | 平均持有 {tr['hold'].mean():.1f} 日")
    print(f"  出场原因: {dict(tr['reason'].value_counts())}")

    # ── 终审审计 ──
    print("\n===== 时间集中度审计 =====")
    by_day = tr.groupby("信号日").size().sort_values(ascending=False)
    n_days = len(by_day)
    print(f"  独立信号日: {n_days} 天 | Top1日 {by_day.iloc[0]} 笔（{by_day.iloc[0]/len(tr):.0%}）"
          f" | Top5日 {by_day.head(5).sum()/len(tr):.0%}")
    daily_x = tr.groupby("信号日").apply(lambda g: g["ret_net"].mean())
    t_day = daily_x.mean()/(daily_x.std()/np.sqrt(len(daily_x))) if len(daily_x) > 5 else np.nan
    t_ev = tr["ret_net"].mean()/(tr["ret_net"].std()/np.sqrt(len(tr)))
    print(f"  单笔净收益 t: 事件口径 {t_ev:.1f} → 独立日口径 {t_day:.1f}")
    print(f"  独立日均值 {daily_x.mean():+.2%}（win {np.mean(daily_x>0):.0%}，n={len(daily_x)}）")
    # 月份分布
    by_month = tr.groupby(tr["信号日"].dt.strftime("%Y-%m")).size().sort_values(ascending=False)
    print(f"  月度分布 top8: " + " ".join(f"{m}({n})" for m, n in by_month.head(8).items()))
    # 与 S1s 重叠
    evx = ev.copy()
    s1s_mask = (evx["direction"]=="DOWN") & (evx["main_net_pct"]>=0.05) & (evx["small_net_pct"]<=-0.05)
    s1s_set = set(zip(evx[s1s_mask]["code"], evx[s1s_mask]["date"]))
    sig_set = set(zip(sig["code"], sig["date"]))
    print(f"  与 S1s 重叠: {len(s1s_set & s1s_set)} / 本策略 {len(sig_set)} / S1s {len(s1s_set)}")

    # 净值保存
    eq.rename("用户策略").to_frame().to_csv(TMP / "user_strategy_equity.csv")
    tr_out = tr.copy()
    tr_out["信号日"] = tr_out["信号日"].dt.strftime("%Y-%m-%d")
    tr_out["买入日"] = tr_out["买入日"].dt.strftime("%Y-%m-%d")
    tr_out["卖出日"] = tr_out["卖出日"].dt.strftime("%Y-%m-%d")
    tr_out.round(4).to_csv(ROOT / "reports" / "用户策略交易明细.csv",
                           index=False, encoding="utf-8-sig")
    print(f"\n明细 → reports/用户策略交易明细.csv")


if __name__ == "__main__":
    main()
