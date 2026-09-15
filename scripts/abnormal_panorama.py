"""全景指标：所有方向×核心子集的 xexec_1/2/3/5/10/20/60 + t + 分年 + 月度夏普 + CAR 曲线。

新增 D7（用户方向）：异动日非 20 日最高量 & 主力无大量流出（main_net_pct >= -2%）
& 当前收盘距最近 20 日天量日收盘回落 > 16%（"天量后深回调+主力未走"洗盘刻画）。

口径：官方事件表（非ST、剔一字板）；xexec_k = T+1 开盘→T+k 收盘 − 当日全市场
同口径均值；月度夏普 = 事件按月聚合的平均 x10 序列 mean/std×sqrt(12)。
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "reports" / "_tmp"
WIN = [1, 2, 3, 5, 10, 20, 60]


def build_panel() -> pd.DataFrame:
    dp = pd.read_parquet(TMP / "daily_panel.parquet",
                         columns=["date", "code", "open", "close", "vol", "ret",
                                  "adj_factor", "vol_ratio"])
    dp = dp.sort_values(["code", "date"]).reset_index(drop=True)
    g = dp.groupby("code", sort=False)
    # 全窗口 exec（T+1 开盘 → T+k 收盘，复权）
    o1a = g["open"].shift(-1) * g["adj_factor"].shift(-1)
    for k in WIN:
        ck = g["close"].shift(-k) * g["adj_factor"].shift(-k)
        dp[f"exec_{k}"] = ck / o1a - 1
    # 市场 ret（日截面均值，用于 CAR）
    dp["mkt_ret"] = dp.groupby("date")["ret"].transform("mean")
    # 20 日天量日特征：最近一次 20 日天量日的收盘价（含当日并列口径）
    dp["vol_20d_max"] = (dp.groupby("code")["vol"]
                         .transform(lambda s: s.rolling(20, min_periods=10).max()))
    is_vd = dp["vol"] >= dp["vol_20d_max"]
    dp["volday_close"] = dp["close"].where(is_vd)
    dp["volday_close"] = dp.groupby("code")["volday_close"].ffill()
    dp["drop_from_volday"] = (
        dp["volday_close"] - dp["close"]) / dp["volday_close"]
    dp["is_20d_high_vol"] = is_vd
    return dp


def build_events(dp: pd.DataFrame) -> pd.DataFrame:
    ev = pd.read_parquet(TMP / "abnormal_events_official.parquet")
    ev = ev[~ev["is_st"].fillna(False).astype(bool)].copy()
    ev = ev[~ev["entry_blocked"].fillna(False)]
    # 全窗口 exec + D7 特征
    cols = ["date", "code", "is_20d_high_vol", "drop_from_volday"] + [f"exec_{k}" for k in WIN]
    ev = ev.merge(dp[cols], on=["date", "code"], how="left", suffixes=("", "_p"))
    # 市场基准（同口径）
    mkt = dp.groupby("date")[[f"exec_{k}" for k in WIN]].mean()
    mkt.columns = [f"mkt_{k}" for k in WIN]
    ev = ev.merge(mkt, on="date", how="left")
    for k in WIN:
        ev[f"x{k}"] = ev[f"exec_{k}"] - ev[f"mkt_{k}"]
    # 席位（拉萨）
    con = duckdb.connect(str(ROOT / "data" / "quant.duckdb"), read_only=True)
    seat = con.execute("""
        select date, code,
               sum(case when dept_name like '%拉萨%' then net else 0 end) lhasa_net
        from dragon_seats group by 1, 2
    """).fetchdf()
    con.close()
    seat["date"] = pd.to_datetime(seat["date"])
    ev = ev.merge(seat, on=["date", "code"], how="left")
    ev["lhasa_pct"] = ev["lhasa_net"] / ev["amount"]
    # 板块密度
    con = duckdb.connect()
    bm = con.execute("""
        select board_code, code from read_parquet(
        'data/lake/clean/board/board_member/snap=2026-09-11/part-d20260911.parquet',
        hive_partitioning=false)
    """).fetchdf()
    con.close()
    evb = ev.merge(bm, on="code", how="inner")
    evb["sector_n"] = evb.groupby(["board_code", "date"])["code"].transform("count")
    evb = (evb.sort_values("sector_n", ascending=False)
              .drop_duplicates(["date", "code"])[["date", "code", "sector_n"]])
    ev = ev.merge(evb, on=["date", "code"], how="left")
    ev["year"] = ev["date"].dt.year
    ev["ym"] = ev["date"].dt.strftime("%Y-%m")
    return ev


def subsets(ev: pd.DataFrame) -> dict[str, pd.Series]:
    up, dn = ev["direction"] == "UP", ev["direction"] == "DOWN"
    hv = ev["is_20d_high_vol"] == True  # noqa: E712
    deep = ev["dev3"] <= -0.29
    # D7（口径按用户修正 2026-09-13）：异动日非 20 日天量 & 主力无大量流出(≥-2%)
    # 价差相对 20 日天量日收盘：UP 档=上涨>16%（天量后继续强势）；DOWN 档=回落>3%
    # drop_from_volday = (天量日收盘 - 当前收盘)/天量日收盘（正=回落，负=上涨）
    d7_up = ((~hv) & (ev["main_net_pct"] >= -0.02)
             & (ev["drop_from_volday"] < -0.16))          # 上涨超16%
    d7_dn = ((~hv) & (ev["main_net_pct"] >= -0.02)
             & (ev["drop_from_volday"] > 0.03))           # 回落超3%
    return {
        "UP 全体": up,
        "UP 天量": up & hv,
        "UP T+1高开>+2%": up & (ev["gap_open"] > 0.02),
        "UP 拉萨净买≥2%": up & (ev["lhasa_pct"] >= 0.02),
        "UP 板块爆发≥7": up & (ev["sector_n"] >= 7),
        "UP 孤立异动": up & (ev["sector_n"] == 1),
        "UP D7天量后涨>16%": up & d7_up,
        "DOWN 全体": dn,
        "DOWN 深度档dev3≤-29%": dn & deep,
        "DOWN S1s强承接": dn & (ev["main_net_pct"] >= 0.05) & (ev["small_net_pct"] <= -0.05),
        "DOWN D6天量+主力净买": dn & hv & (ev["main_net_pct"] > 0),
        "DOWN 主力净卖(对照)": dn & (ev["main_net_pct"] <= -0.02),
        "DOWN 板块爆发≥7": dn & (ev["sector_n"] >= 7),
        "DOWN D7天量后回落>3%": dn & d7_dn,
    }


def report(ev: pd.DataFrame) -> None:
    ss = subsets(ev)
    print("=" * 132)
    print("全景指标表：n | x1/x2/x3/x5/x10/x20/x60（%，市场调整超额）| x10的t | 月度夏普 | 分年x10")
    print("=" * 132)
    rows = []
    for name, m in ss.items():
        sub = ev[m.reindex(ev.index).fillna(False)]
        if len(sub) == 0:
            continue
        r = {"子集": name, "n": len(sub)}
        for k in WIN:
            s = sub[f"x{k}"].dropna()
            r[f"x{k}"] = s.mean() if len(s) else np.nan
        s10 = sub["x10"].dropna()
        t10 = s10.mean() / (s10.std() / np.sqrt(len(s10))) if len(s10) > 5 and s10.std() > 0 else np.nan
        r["t(x10)"] = t10
        # 月度夏普（月内事件平均 x10 → 月序列）
        mm = sub.groupby("ym")["x10"].mean().dropna()
        r["夏普"] = mm.mean() / mm.std() * np.sqrt(12) if len(mm) > 5 and mm.std() > 0 else np.nan
        for y in [2022, 2023, 2024, 2025, 2026]:
            sy = sub[sub["year"] == y]["x10"].dropna()
            r[f"{y}"] = sy.mean() if len(sy) >= 5 else np.nan
        rows.append(r)
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    fmt = df.copy()
    for c in [f"x{k}" for k in WIN] + ["t(x10)", "夏普", "2022", "2023", "2024", "2025", "2026"]:
        fmt[c] = fmt[c].map(lambda v: (f"{v:+.1f}" if c in ("t(x10)", "夏普")
                                       else f"{v*100:+.1f}") if pd.notna(v) else "—")
    fmt["n"] = fmt["n"].astype(int)
    print(fmt.to_string(index=False))
    df.to_csv(TMP / "panorama_table.csv", index=False, encoding="utf-8-sig")
    print(f"\n→ {TMP / 'panorama_table.csv'}")


def car_curves(ev: pd.DataFrame, dp: pd.DataFrame) -> None:
    """CAR：事件对齐 T+1..T+60 累计平均日超额收益（%）。按 k 单独 nanmean，
    早期事件（持有期未达 60 日）仍能对短 k 贡献。"""
    dp = dp.sort_values(["code", "date"]).reset_index(drop=True)
    pos = {(c, d): i for i, (c, d) in enumerate(
        zip(dp["code"], dp["date"]))}
    rets = dp["ret"].to_numpy()
    mrets = dp["mkt_ret"].to_numpy()
    K = 60

    def car(mask):
        sub = ev[mask.reindex(ev.index).fillna(False)]
        # 收集每事件的 T+1..T+K 日超额
        per_event = np.full((len(sub), K), np.nan)
        for r, (_, e) in enumerate(sub.iterrows()):
            i = pos.get((e["code"], e["date"]))
            if i is None:
                continue
            for k in range(1, K + 1):
                j = i + k
                if j >= len(dp):
                    break
                per_event[r, k - 1] = rets[j] - mrets[j]
        # 按 k 单独 nanmean，再 cumsum ×100
        return np.cumsum(np.nanmean(per_event, axis=0)) * 100

    ss = subsets(ev)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.family"] = "Microsoft YaHei"
    plt.rcParams["axes.unicode_minus"] = False
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5), sharey=True)
    up_sets = ["UP 全体", "UP 天量", "UP T+1高开>+2%", "UP D7天量后涨>16%"]
    dn_sets = ["DOWN 全体", "DOWN S1s强承接", "DOWN D6天量+主力净买",
               "DOWN 深度档dev3≤-29%", "DOWN D7天量后回落>3%"]
    colors = plt.cm.tab10.colors
    for ax, names, title in [(axes[0], up_sets, "UP（连板急涨）"),
                             (axes[1], dn_sets, "DOWN（3日深跌）")]:
        for c, nm in zip(colors, names):
            if nm not in ss:
                continue
            y = car(ss[nm])
            ax.plot(range(1, len(y) + 1), y, label=f"{nm} (n={(ss[nm]).sum()})",
                    color=c, lw=1.6)
        ax.axhline(0, color="gray", lw=0.8, ls="--")
        ax.set_title(f"{title}：事件后累计平均日超额（CAR，%）")
        ax.set_xlabel("事件后交易日")
        ax.set_xlim(1, 60)
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("CAR %（T+1 开盘起，市场调整）")
    fig.suptitle("严重异动事件 CAR 曲线（市场调整超额，官方口径 2022-2026）", y=1.02)
    out = ROOT / "reports" / "abnormal_car_curves.png"
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    print(f"CAR 曲线 → {out}")


def main() -> None:
    dp = build_panel()
    ev = build_events(dp)
    report(ev)
    car_curves(ev, dp)
    ev.to_parquet(TMP / "abnormal_events_panorama.parquet", index=False)


if __name__ == "__main__":
    main()
