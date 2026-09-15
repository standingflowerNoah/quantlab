"""严重异动新方向 D1-D6 事件层正式研究（官方口径事件表）。

统一口径：abnormal_events_official.parquet（非ST）、T+1 开盘入场、
xexec 市场调整（当日全市场截面均值）、分年稳定性。
每个方向只测事前有经济含义的分层，避免数据挖掘。

D1 异动后 T+1 量价形态：量比分档 × 方向（抛压衰竭 vs 分歧派发）
D2 龙虎榜席位结构：机构/拉萨散户/券商营业部三类净买分层；拉萨过滤器
D3 连续异动生命周期：streak 衰减 + DOWN 第4次反转扩样
D4 板块异动密度：同板块当日异动数（快照板块映射，PIT caveat 声明）
D5 次日情绪延续：T+1 跳空分档 × 方向
D6 20日天量+主力净流入：天量 × 资金流方向交叉
"""
from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "reports" / "_tmp"
HORIZONS = [3, 10, 20]


def st(x, label_width=0):
    x = pd.Series(x).dropna()
    if len(x) < 15:
        return f"n={len(x)}(少)"
    t = x.mean() / (x.std() / np.sqrt(len(x))) if x.std() > 0 else np.nan
    return (f"n={len(x):>5} mean={x.mean():+.2%} med={x.median():+.2%} "
            f"win={np.mean(x > 0):.0%} t={t:.1f}")


def yearly(df, col, mask):
    out = []
    sub = df[mask]
    for y, g in sub.groupby(sub["date"].dt.year):
        s = g[col].dropna()
        if len(s) >= 5:
            out.append(f"{y}:{s.mean():+.1%}({len(s)})")
    return "  ".join(out) if out else "样本不足"


def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    ev = pd.read_parquet(TMP / "abnormal_events_official.parquet")
    ev = ev[~ev["is_st"].fillna(False).astype(bool)].copy()
    ev = ev[~ev["entry_blocked"].fillna(False)]
    dp = pd.read_parquet(TMP / "daily_panel.parquet",
                         columns=["date", "code", "open", "close", "vol", "ret",
                                  "adj_factor", "exec_10", "exec_20", "vol_ratio"])
    # exec_3 面板没有 → 重算（T+1 开盘 → T+3 收盘）
    dp = dp.sort_values(["code", "date"]).reset_index(drop=True)
    g0 = dp.groupby("code", sort=False)
    o1 = g0["open"].shift(-1)
    a1 = g0["adj_factor"].shift(-1)
    c3 = g0["close"].shift(-3) * g0["adj_factor"].shift(-3)
    dp["exec_3"] = c3 / (o1 * a1) - 1
    mkt = dp.groupby("date")[["exec_3", "exec_10", "exec_20"]].mean()
    mkt.columns = [f"mkt_{h}" for h in HORIZONS]
    ev = ev.merge(dp[["date", "code", "exec_3"]], on=["date", "code"], how="left")
    ev = ev.merge(mkt, on="date", how="left")
    for h in HORIZONS:
        ev[f"x{h}"] = ev[f"exec_{h}"] - ev[f"mkt_{h}"]
    # T+1 量比（T+1 收盘可知）
    dp2 = dp.sort_values(["code", "date"]).reset_index(drop=True)
    g = dp2.groupby("code", sort=False)
    dp2["t1_volratio"] = g["vol_ratio"].shift(-1)
    # 20 日滚动最高量（含当日）：当日是否天量
    dp2["vol_20d_max"] = (dp2.groupby("code", sort=False)["vol"]
                          .transform(lambda s: s.rolling(20, min_periods=10).max()))
    dp2["is_20d_high_vol"] = dp2["vol"] >= dp2["vol_20d_max"]
    ev = ev.merge(dp2[["date", "code", "t1_volratio", "is_20d_high_vol"]],
                  on=["date", "code"], how="left")
    # streak（gap>3 自然日断开）
    ev = ev.sort_values(["code", "date"]).reset_index(drop=True)
    prev = ev.groupby("code")["date"].shift(1)
    brk = prev.isna() | ((ev["date"] - prev).dt.days > 3)
    ev["streak"] = brk.cumsum().groupby(brk.cumsum()).cumcount() + 1
    ev["year"] = ev["date"].dt.year
    return ev


def d1_t1_volume(ev):
    print("\n" + "=" * 100)
    print("D1 | 异动后 T+1 量比形态（T+1 收盘可知 → T+2 开盘入场口径的前瞻）")
    print("=" * 100)
    bins = [0, 0.8, 1.2, 1.7, 3.0, 100]
    labels = ["<0.8 显著缩量", "0.8-1.2 平量", "1.2-1.7 温和放量",
              "1.7-3 放量", ">3 巨量"]
    ev["t1v_bin"] = pd.cut(ev["t1_volratio"], bins=bins, labels=labels)
    for d_ in ["UP", "DOWN"]:
        sub = ev[ev["direction"] == d_]
        print(f"\n-- {d_} --")
        for b in labels:
            s = sub[sub["t1v_bin"] == b]
            print(f"  {b:<14}: x10 {st(s['x10'])}")
    print(f"\n  分年（DOWN × 1.2-1.7 档 x10）: {yearly(ev, 'x10', (ev['direction']=='DOWN') & (ev['t1v_bin']=='1.2-1.7 温和放量'))}")
    print(f"  分年（DOWN × <0.8 档 x10）:     {yearly(ev, 'x10', (ev['direction']=='DOWN') & (ev['t1v_bin']=='<0.8 显著缩量'))}")


def d2_seats(ev):
    print("\n" + "=" * 100)
    print("D2 | 龙虎榜席位结构（机构 / 拉萨散户通道 / 券商营业部）")
    print("=" * 100)
    con = duckdb.connect(str(ROOT / "data" / "quant.duckdb"), read_only=True)
    seat = con.execute("""
        select date, code,
               sum(case when is_inst then net else 0 end) inst_net,
               sum(case when dept_name like '%拉萨%' then net else 0 end) lhasa_net,
               sum(case when not is_inst and dept_name not like '%拉萨%'
                        then net else 0 end) broker_net
        from dragon_seats group by 1, 2
    """).fetchdf()
    con.close()
    seat["date"] = pd.to_datetime(seat["date"])
    m = ev.merge(seat, on=["date", "code"], how="inner")
    print(f"事件 join 席位: {len(m):,} / {len(ev):,}（{len(m)/len(ev):.0%}，仅上榜事件）")
    for c in ["inst_net", "lhasa_net", "broker_net"]:
        m[f"{c}_pct"] = m[c] / m["amount"]
    for d_ in ["UP", "DOWN"]:
        sub = m[m["direction"] == d_]
        print(f"\n-- {d_}（上榜 n={len(sub)}）--")
        for c, nm in [("inst_net", "机构净买"), ("lhasa_net", "拉萨净买"),
                      ("broker_net", "营业部净买")]:
            hi = sub[sub[f"{c}_pct"] >= 0.02]
            lo = sub[sub[f"{c}_pct"] <= -0.02]
            print(f"  {nm}≥+2%: x10 {st(hi['x10'])}")
            print(f"  {nm}≤-2%: x10 {st(lo['x10'])}")
    lh = m[m["lhasa_net_pct"] >= 0.02]
    print(f"\n  分年（拉萨净买≥2% 全方向 x10）: {yearly(m, 'x10', m['lhasa_net_pct'] >= 0.02)}")
    # 拉萨过滤器对 S1s 的影响
    s1s_mask = ((ev["direction"] == "DOWN") & (ev["main_net_pct"] >= 0.05)
                & (ev["small_net_pct"] <= -0.05))
    ev_m = ev.merge(seat[["date", "code", "lhasa_net"]], on=["date", "code"], how="left")
    ev_m["lhasa_pct"] = ev_m["lhasa_net"] / ev_m["amount"]
    s1s = ev_m[s1s_mask.reindex(ev_m.index).fillna(False)]
    n_all = s1s["x10"].dropna()
    kept = s1s[~(s1s["lhasa_pct"] >= 0.02)]["x10"].dropna()
    print(f"\n  拉萨过滤器 × S1s：全 S1s x10={n_all.mean():+.2%}(n={len(n_all)})"
          f" → 剔除拉萨净买≥2%后 {kept.mean():+.2%}(n={len(kept)})")
    return m


def d3_streak(ev):
    print("\n" + "=" * 100)
    print("D3 | 连续异动生命周期（streak 衰减）")
    print("=" * 100)
    for d_ in ["UP", "DOWN"]:
        sub = ev[ev["direction"] == d_]
        print(f"\n-- {d_} --")
        for k in [1, 2, 3, 4]:
            s = sub[sub["streak"] == k]
            print(f"  第{k}次: x10 {st(s['x10'])}")
        s5 = sub[sub["streak"] >= 5]
        print(f"  第5次+: x10 {st(s5['x10'])}")
    print(f"\n  分年（UP streak≥3 x10）: {yearly(ev, 'x10', (ev['direction']=='UP') & (ev['streak']>=3))}")
    dn4 = ev[(ev["direction"] == "DOWN") & (ev["streak"] >= 4)]
    print(f"  DOWN streak≥4（反转苗头扩样）: x10 {st(dn4['x10'])}")
    print(f"  分年: {yearly(ev, 'x10', (ev['direction']=='DOWN') & (ev['streak']>=4))}")


def d4_sector_density(ev):
    print("\n" + "=" * 100)
    print("D4 | 板块异动密度（⚠️ 板块映射为 2026-09 快照，PIT 近似，仅初步）")
    print("=" * 100)
    con = duckdb.connect()
    bm = con.execute("""
        select board_code, code from read_parquet(
          'data/lake/clean/board/board_member/snap=2026-09-11/part-d20260911.parquet',
          hive_partitioning=false)
    """).fetchdf()
    con.close()
    evb = ev.merge(bm, on="code", how="inner")
    # 每板块成员数
    bsize = bm.groupby("board_code")["code"].nunique()
    evb["bsize"] = evb["board_code"].map(bsize)
    evb = evb[evb["bsize"] >= 20]  # 太小板块无意义
    # 当日同板块异动事件数
    dens = evb.groupby(["board_code", "date"])["code"].transform("count")
    evb["sector_n"] = dens
    # 一股属多板块 → 取密度最大的一行去重
    evb = evb.sort_values("sector_n", ascending=False).drop_duplicates(["date", "code"])
    print(f"join 板块成功: {len(evb):,}")
    evb["dens_bin"] = pd.cut(evb["sector_n"], [0, 1, 3, 6, 1000],
                             labels=["1 孤立异动", "2-3 少数", "4-6 板块效应", "7+ 板块爆发"])
    for d_ in ["UP", "DOWN"]:
        sub = evb[evb["direction"] == d_]
        print(f"\n-- {d_} --")
        for b in ["1 孤立异动", "2-3 少数", "4-6 板块效应", "7+ 板块爆发"]:
            s = sub[sub["dens_bin"] == b]
            print(f"  {b:<12}: x10 {st(s['x10'])}")
    print(f"\n  分年（UP × 7+ 板块爆发 x10）: {yearly(evb, 'x10', (evb['direction']=='UP') & (evb['dens_bin']=='7+ 板块爆发'))}")


def d5_gap(ev):
    print("\n" + "=" * 100)
    print("D5 | 次日情绪延续：T+1 跳空分档 × 方向")
    print("=" * 100)
    bins = [-1, -0.02, 0.0, 0.02, 1]
    labels = ["低开<-2%", "-2~0%", "0~+2%", "高开>+2%"]
    ev["gap_bin"] = pd.cut(ev["gap_open"], bins=bins, labels=labels)
    for d_ in ["UP", "DOWN"]:
        sub = ev[ev["direction"] == d_]
        print(f"\n-- {d_} --")
        for b in labels:
            s = sub[sub["gap_bin"] == b]
            print(f"  {b:<9}: x3 {st(s['x3']):<58} x10 {st(s['x10'])}")
    print(f"\n  分年（DOWN × 高开>+2% x10）: {yearly(ev, 'x10', (ev['direction']=='DOWN') & (ev['gap_bin']=='高开>+2%'))}")
    print(f"  分年（UP × 高开>+2% x10）:   {yearly(ev, 'x10', (ev['direction']=='UP') & (ev['gap_bin']=='高开>+2%'))}")


def d6_20d_high_vol(ev):
    print("\n" + "=" * 100)
    print("D6 | 异动当日创 20 日最高量 × 主力净流入（新增方向）")
    print("=" * 100)
    print(f"\n天量占比: UP {ev[ev['direction']=='UP']['is_20d_high_vol'].mean():.0%} / "
          f"DOWN {ev[ev['direction']=='DOWN']['is_20d_high_vol'].mean():.0%}")
    for d_ in ["UP", "DOWN"]:
        sub = ev[ev["direction"] == d_]
        hv = sub[sub["is_20d_high_vol"] == True]  # noqa: E712
        nhv = sub[sub["is_20d_high_vol"] == False]  # noqa: E712
        print(f"\n-- {d_} --")
        print(f"  全体对照:        x10 {st(sub['x10'])}")
        print(f"  天量(无条件):    x10 {st(hv['x10'])}")
        print(f"  非天量:          x10 {st(nhv['x10'])}")
        hv_buy = hv[hv["main_net_pct"] > 0]
        hv_sell = hv[hv["main_net_pct"] <= 0]
        print(f"  天量+主力净买:   x10 {st(hv_buy['x10'])}")
        print(f"  天量+主力净卖:   x10 {st(hv_sell['x10'])}")
        # 强承接细分
        hv_buy5 = hv[hv["main_net_pct"] >= 0.05]
        print(f"  天量+主力≥5%:    x10 {st(hv_buy5['x10'])}")
    sig = (ev["is_20d_high_vol"] == True) & (ev["main_net_pct"] > 0)  # noqa: E712
    for d_ in ["UP", "DOWN"]:
        print(f"\n  分年（{d_} × 天量+主力净买 x10）: "
              f"{yearly(ev, 'x10', sig & (ev['direction']==d_))}")
    # 与思路1/2 的关系
    s1 = (ev["direction"] == "DOWN") & (ev["main_net_pct"] >= 0.05) & (ev["small_net_pct"] <= -0.05)
    print(f"\n  与 S1s 重叠: {(sig & s1).sum()} / S1s={s1.sum()} / D6信号(DOWN)={(sig & (ev['direction']=='DOWN')).sum()}")


def main() -> None:
    ev = load()
    print(f"官方口径事件（非ST、剔一字板）: {len(ev):,} "
          f"(UP {(ev['direction']=='UP').sum():,} / DOWN {(ev['direction']=='DOWN').sum():,})")
    d1_t1_volume(ev)
    d2_seats(ev)
    d3_streak(ev)
    d4_sector_density(ev)
    d5_gap(ev)
    d6_20d_high_vol(ev)
    ev.to_parquet(TMP / "abnormal_events_official_enriched.parquet", index=False)
    print(f"\n增强事件表 → {TMP / 'abnormal_events_official_enriched.parquet'}")


if __name__ == "__main__":
    main()
