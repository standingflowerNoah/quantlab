"""S1s 动态出场策略回测：出信号 T+1 开盘全仓等分买入，主力大幅流出日卖出。

出场规则
--------
- 买入：信号日 T 次日（T+1）开盘价买入
- 卖出：T+2 起每日收盘检查该股当日 main_net_pct ≤ -5%（主力大幅流出，
  与买入阈值对称）→ 当日收盘卖出（实盘近似：收盘前 14:55 判断当日
  资金流已基本定型，可执行；次日开盘卖作敏感性对照）
- 兜底：持有 60 交易日强制卖出；数据尾部截断
- 一字涨停卖出可行性：卖出方向涨停是利好可成交，不剔除；跌停无法卖出
  的情形未建模（声明）

组合与成本
----------
- 日历等权组合：当日所有开放持仓等权（"全仓均分"口径）
- 成本：买=佣金2.5bp+滑点20bp=22.5bp；卖=佣金2.5bp+印花5bp+滑点20bp=27.5bp
- 按当日持仓数摊销到组合日收益

变体：S1s 无条件 / S1s×危机态（微盘20日≤-10%）/ 出场阈值 -2%/-5%/-8% 敏感性
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "reports" / "_tmp"
MF_DIR = ROOT / "data" / "lake" / "clean" / "moneyflow"

EXIT_TH = -0.05       # 主力大幅流出阈值（主口径 -5%）
MAX_HOLD = 60         # 兜底持有期
COST_BUY = 0.00225    # 22.5bp
COST_SELL = 0.00275   # 27.5bp


def load_panel() -> pd.DataFrame:
    dp = pd.read_parquet(TMP / "daily_panel.parquet",
                         columns=["date", "code", "open", "close", "ret", "adj_factor", "amount"])
    files = sorted(MF_DIR.glob("part-*.parquet"))
    if files:
        con = duckdb.connect()
        fs = ", ".join(f"'{f.as_posix()}'" for f in files)
        mf_raw = con.execute(f"""
            select date, code, main_net from read_parquet([{fs}], hive_partitioning=false)
        """).fetchdf()
        con.close()
        mf_raw["date"] = pd.to_datetime(mf_raw["date"])
        dp = dp.merge(mf_raw, on=["date", "code"], how="left")
        dp["main_net_pct"] = dp["main_net"] * 1e4 / dp["amount"]
    dp = dp.sort_values(["code", "date"]).reset_index(drop=True)
    return dp


def simulate(ev: pd.DataFrame, dp: pd.DataFrame, exit_th: float,
             label: str) -> dict:
    """逐信号持仓路径 → 日历等权组合净值。"""
    # 面板索引：code → (date → row_idx)
    pos = {}
    codes = dp["code"].to_numpy()
    dates = dp["date"].to_numpy()
    idx_map = {}
    for i, (c, d) in enumerate(zip(codes, dates)):
        idx_map[(c, d)] = i
    rets = dp["ret"].to_numpy()
    opens = dp["open"].to_numpy()
    closes = dp["close"].to_numpy()
    mfp = dp["main_net_pct"].to_numpy() if "main_net_pct" in dp else np.full(len(dp), np.nan)

    trades = []  # (entry_idx, exit_idx, entry_ret(日内), reason)
    for _, e in ev.iterrows():
        i0 = idx_map.get((e["code"], e["date"]))
        if i0 is None or i0 + 1 >= len(dp) or codes[i0 + 1] != e["code"]:
            continue
        i1 = i0 + 1
        entry_day_ret = closes[i1] / opens[i1] - 1     # T+1 开盘→收盘
        exit_i, reason = None, None
        for k in range(i1 + 1, min(i1 + MAX_HOLD, len(dp))):
            if codes[k] != e["code"]:
                break
            if not np.isnan(mfp[k]) and mfp[k] <= exit_th:
                exit_i, reason = k, "主力流出"
                break
        if exit_i is None:
            # 兜底：60日 or 尾部
            end = min(i1 + MAX_HOLD, len(dp) - 1)
            j = end
            while j > i1 and codes[j] != e["code"]:
                j -= 1
            exit_i = j
            reason = "60日兜底" if (exit_i - i1) >= MAX_HOLD - 1 else "尾部截断"
        trades.append((i1, exit_i, entry_day_ret, reason))

    # 展开为逐日收益
    rows = []
    for t_i, (i1, iex, r0, reason) in enumerate(trades):
        rows.append((dates[i1], t_i, r0, 0))
        for k in range(i1 + 1, iex + 1):
            if codes[k] != codes[i1]:
                break
            rows.append((dates[k], t_i, rets[k] if not np.isnan(rets[k]) else 0.0,
                         1 if k == iex else 0))
    pos_df = pd.DataFrame(rows, columns=["date", "tid", "ret", "is_exit"])
    pos_df["date"] = pd.to_datetime(pos_df["date"])

    daily = pos_df.groupby("date").agg(ret=("ret", "mean"),
                                       n=("tid", "nunique")).reset_index()
    # 成本摊销
    entries = pos_df.drop_duplicates("tid").groupby("date").size()
    exits = pos_df[pos_df["is_exit"] == 1].groupby("date").size()
    n_open = daily.set_index("date")["n"]
    adj = pd.Series(0.0, index=daily["date"])
    for d, cnt in entries.items():
        if d in adj.index and n_open[d] > 0:
            adj[d] += COST_BUY * cnt / n_open[d]
    for d, cnt in exits.items():
        if d in adj.index and n_open[d] > 0:
            adj[d] += COST_SELL * cnt / n_open[d]
    daily["net"] = daily["ret"] - adj.values

    eq = (1 + daily.set_index("date")["net"]).cumprod()
    yrs = (daily["date"].max() - daily["date"].min()).days / 365.25
    ann = eq.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    sharpe = daily["net"].mean() / daily["net"].std() * np.sqrt(244) if daily["net"].std() > 0 else np.nan
    yearly = eq.groupby(eq.index.year).apply(lambda x: x.iloc[-1] / x.iloc[0] - 1)
    # 持有天数与卖出原因
    hold_days = [iex - i1 for i1, iex, _, _ in trades]
    reasons = pd.Series([r for _, _, _, r in trades]).value_counts()
    return {"label": label, "n_trades": len(trades), "ann": ann, "dd": dd,
            "sharpe": sharpe, "yearly": yearly, "eq": eq,
            "avg_hold": np.mean(hold_days), "reasons": reasons,
            "daily": daily.set_index("date")}


def main() -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.family"] = "Microsoft YaHei"
    plt.rcParams["axes.unicode_minus"] = False

    ev = pd.read_parquet(TMP / "abnormal_events_official_enriched.parquet")
    ev = ev[~ev["is_st"].fillna(False).astype(bool)].copy()
    ev = ev[~ev["entry_blocked"].fillna(False)]
    # 危机态
    con = duckdb.connect(str(ROOT / "data" / "quant.duckdb"), read_only=True)
    idx = con.execute("select date, close from index_kline where code='880003.SH' order by date").fetchdf()
    con.close()
    idx["date"] = pd.to_datetime(idx["date"])
    idx = idx.sort_values("date").reset_index(drop=True)
    idx["ret20"] = idx["close"] / idx["close"].shift(20) - 1
    ev = ev.merge(idx[["date", "ret20"]], on="date", how="left")

    dp = load_panel()
    dn = ev[ev["direction"] == "DOWN"]
    s1s = dn[(dn["main_net_pct"] >= 0.05) & (dn["small_net_pct"] <= -0.05)]

    results = []
    for lbl, sub, th in [
        ("S1s 无条件｜流出-5%卖", s1s, -0.05),
        ("S1s×危机态｜流出-5%卖", s1s[s1s["ret20"] <= -0.10], -0.05),
        ("S1s 无条件｜流出-2%卖", s1s, -0.02),
        ("S1s 无条件｜流出-8%卖", s1s, -0.08),
    ]:
        r = simulate(sub, dp, th, lbl)
        results.append(r)
        print(f"\n===== {lbl} =====")
        print(f"  交易 {r['n_trades']} 笔 | 年化 {r['ann']:+.1%} | 回撤 {r['dd']:.1%} | "
              f"夏普 {r['sharpe']:.2f} | 平均持有 {r['avg_hold']:.0f} 日")
        print("  分年: " + "  ".join(f"{y}:{v:+.1%}" for y, v in r["yearly"].items()))
        print("  卖出原因: " + "  ".join(f"{k}:{v}" for k, v in r["reasons"].items()))

    # 净值曲线
    fig, axes = plt.subplots(1, 2, figsize=(15, 5.5),
                             gridspec_kw={"width_ratios": [2.2, 1]})
    colors = ["#c23531", "#2d4470", "#e08a3c", "#7b4fa6"]
    for c, r in zip(colors, results):
        axes[0].plot(r["eq"].index, r["eq"].values, label=r["label"], color=c, lw=1.6)
    axes[0].axhline(1.0, color="gray", lw=0.8, ls="--")
    axes[0].set_title("S1s 动态出场策略净值（全仓等权、成本后，2022-2026）")
    axes[0].set_ylabel("净值")
    axes[0].legend(fontsize=9)
    axes[0].grid(alpha=0.3)
    # 分年柱状（主口径）
    main_r = results[0]
    ys = main_r["yearly"].index.astype(int)
    axes[1].bar([str(y) for y in ys], main_r["yearly"].values * 100,
                color=["#c23531" if v > 0 else "#2f7d4f" for v in main_r["yearly"].values])
    axes[1].set_title(f"主口径分年收益：{main_r['label']}")
    axes[1].set_ylabel("%")
    axes[1].grid(alpha=0.3, axis="y")
    fig.tight_layout()
    out = ROOT / "reports" / "abnormal_s1s_dynamic_exit.png"
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"\n净值曲线 → {out}")


if __name__ == "__main__":
    main()
