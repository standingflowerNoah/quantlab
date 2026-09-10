r"""买入时点研究 —— 后处理与统计（消费 cell_main.parquet）
=================================================================
已定位的数据陷阱（必须排除）：
  1) 2026 年存在 mod=780（13:00）的**污染 bar**：仅覆盖约 90 只 688 段次新/高价股
     的日期上，该 bar 价格与前后 bar 明显脱节，使该时点日均收益虚高到 +222bp
     （t=12.2）——纯数据伪影，全样本加权均值实为 −1.6bp。→ 全表剔除 mod 780。
  2) 2026-06-24 起数据源更换分钟网格：上午末根 bar 由 11:30 变 11:29，
     14:57 之后 bar 成交量趋零（进入收盘集合竞价）。→ 主结论限定在
     09:30–14:56 连续竞价窗口，集合竞价窗口单独报告并标注不可靠。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CELL = ROOT / "data/lake/factor/overnight/entry_timing/cell_main.parquet"
EXT = ROOT / "data/lake/factor/overnight/entry_timing/curve_daily_ext_main.parquet"
REP = ROOT / "reports/overnight"
OUTJ = REP / "entry_timing_report.json"

BAD_MODS = {780}                     # 污染 bar
AUCTION = {897, 898, 899, 900}       # 收盘集合竞价窗口（不可靠）
CONT_END = 896                       # 14:56 = 最后可信连续竞价时点
MOD_OPEN = 569
KEYS = [569, 571, 575, 585, 600, 615, 630, 645, 660, 675, 690,
        781, 790, 810, 830, 850, 866, 875, 880, 890, 895, 896, 900]
COST = 15.0   # bp/往返


def hm(mod: int) -> str:
    return "09:25" if mod == 569 else f"{mod//60:02d}:{mod%60:02d}"


def merge(df: pd.DataFrame, keys) -> pd.DataFrame:
    g = df.groupby(list(keys), observed=True).agg(
        n=("n", "sum"), s_r=("s_r", "sum"), s_r2=("s_r2", "sum"),
        n_win=("n_win", "sum"), s_intra=("s_intra", "sum"), s_gap=("s_gap", "sum")
    ).reset_index()
    g["mean_ret"] = g.s_r / g.n
    var = (g.s_r2 - g.s_r ** 2 / g.n) / (g.n - 1).clip(lower=1)
    g["winr"] = g.n_win / g.n
    g["mean_intra"] = g.s_intra / g.n
    g["mean_gap"] = g.s_gap / g.n
    return g


def ts(s: pd.Series) -> dict:
    s = pd.Series(s).dropna()
    n = len(s)
    if n < 5:
        return dict(n_days=int(n), mean_bp=None, t=None, win=None, ann=None, sd_bp=None)
    mu, sd = float(s.mean()), float(s.std(ddof=1))
    return dict(n_days=int(n), mean_bp=round(mu * 1e4, 2), sd_bp=round(sd * 1e4, 1),
                t=round(mu / (sd / np.sqrt(n)), 2) if sd > 0 else None,
                win=round(float((s > 0).mean()), 4),
                ann=round(((1 + mu) ** 244 - 1) * 100, 1))


def paired(cur: pd.DataFrame, a: int, b: int) -> dict:
    """同日配对价差 a − b（bp/日）"""
    pa = cur[cur["mod"] == a].set_index("d")["mean_ret"]
    pb = cur[cur["mod"] == b].set_index("d")["mean_ret"]
    d = (pa - pb).dropna()
    mu, sd = float(d.mean()), float(d.std(ddof=1))
    return dict(a=hm(a), b=hm(b), n_days=int(len(d)), spread_bp=round(mu * 1e4, 2),
                t=round(mu / (sd / np.sqrt(len(d))), 2) if sd > 0 else None,
                win=round(float((d > 0).mean()), 4))


def main() -> None:
    cell = pd.read_parquet(CELL)
    cell["d"] = pd.to_datetime(cell["d"])
    n0 = len(cell)
    cell = cell[~cell["mod"].isin(BAD_MODS)]
    print(f"[filter] drop mod {BAD_MODS}: cells {n0:,} → {len(cell):,}")

    cur_nl = merge(cell[~cell.buy_lim], ["d", "mod"])
    cur_all = merge(cell, ["d", "mod"])

    # ── 主曲线 ──
    rows = []
    for m, g in cur_nl.groupby("mod"):
        g = g.set_index("d")
        r = ts(g["mean_ret"])
        r.update(mod=int(m), hm=hm(int(m)),
                 mean_intra_bp=round(float(g["mean_intra"].mean()) * 1e4, 2),
                 mean_gap_bp=round(float(g["mean_gap"].mean()) * 1e4, 2),
                 stock_win=round(float(g["winr"].mean()), 4),
                 net_bp=round((r["mean_bp"] or 0) - COST, 2))
        rows.append(r)
    curve = pd.DataFrame(rows).sort_values("mod").reset_index(drop=True)
    curve.to_csv(REP / "entry_timing_final_curve.csv", index=False, encoding="utf-8-sig")

    cont = curve[curve["mod"] <= CONT_END]
    print(f"[curve] {len(curve)} mods; 连续竞价窗口 {len(cont)} mods")
    print(cont.sort_values("mean_bp", ascending=False).head(5)[
        ["hm", "mean_bp", "t", "win", "ann", "net_bp"]].to_string(index=False))
    print(cont.sort_values("mean_bp").head(5)[
        ["hm", "mean_bp", "t", "win", "ann", "net_bp"]].to_string(index=False))

    # ── 分年 ──
    ytab = {}
    for y, gy in cur_nl.groupby(cur_nl["d"].dt.year):
        ytab[int(y)] = {hm(int(m)): ts(gy[gy["mod"] == m].set_index("d")["mean_ret"])["mean_bp"]
                        for m in KEYS}
    ydf = pd.DataFrame(ytab)
    ydf.index.name = "hm"
    print("\n=== 分年 mean_bp ===")
    print(ydf.to_string())

    # ── 分月（形状稳定性）──
    mm = cur_nl.copy()
    mm["ym"] = mm["d"].dt.to_period("M").astype(str)
    mrows = []
    for ym, g in mm.groupby("ym"):
        r = {"ym": ym}
        for m in [569, 600, 690, 810, 896, 900]:
            gg = g[g["mod"] == m].set_index("d")["mean_ret"]
            r[hm(m)] = round(float(gg.mean()) * 1e4, 1) if len(gg) else None
        mrows.append(r)
    mdf = pd.DataFrame(mrows)
    print("\n=== 分月（关键时点 bp）===")
    print(mdf.to_string(index=False))

    # ── 配对价差 ──
    pairs = [(571, 900), (571, 896), (569, 900), (571, 690), (690, 896),
             (896, 900), (571, 600), (600, 896), (781, 900)]
    ptab = pd.DataFrame([paired(cur_nl, a, b) for a, b in pairs])
    print("\n=== 同日配对价差 ===")
    print(ptab.to_string(index=False))

    # ── 月度配对价差（形状稳定性）──
    pa = cur_nl[cur_nl["mod"] == 571].set_index("d")["mean_ret"]
    pb = cur_nl[cur_nl["mod"] == 900].set_index("d")["mean_ret"]
    pc = cur_nl[cur_nl["mod"] == 896].set_index("d")["mean_ret"]
    sp = pd.DataFrame({"d": pa.index, "open_minus_close": (pa - pb).values,
                       "open_minus_1456": (pa - pc).values}).dropna()
    sp["ym"] = sp["d"].dt.to_period("M").astype(str)
    ms = sp.groupby("ym").agg(
        n=("open_minus_close", "size"),
        open_minus_close=("open_minus_close", lambda x: round(x.mean() * 1e4, 1)),
        open_minus_1456=("open_minus_1456", lambda x: round(x.mean() * 1e4, 1))).reset_index()
    ms["win"] = sp.groupby("ym")["open_minus_close"].apply(lambda x: round(float((x > 0).mean()), 3)).values
    print("\n=== 月度价差（09:31 − 15:00，bp/日）===")
    print(ms.to_string(index=False))
    print(f"月度为正的比例: {(ms.open_minus_close > 0).mean():.1%}  "
          f"均值 {ms.open_minus_close.mean():.1f}bp  最小 {ms.open_minus_close.min():.1f}  "
          f"最大 {ms.open_minus_close.max():.1f}")

    # ── 滚动 / 累计序列（供画图）──
    series = pd.DataFrame({"open": pa, "close": pb, "m1456": pc}).dropna()
    series["spread"] = series["open"] - series["close"]
    series["roll60"] = series["spread"].rolling(60).mean() * 1e4
    series.to_csv(REP / "entry_timing_final_series.csv", encoding="utf-8-sig")
    print(f"\n累计价差（100% 仓位每日重复）: {(1+series.spread).prod()-1:.2%}  "
          f"日价差>0 占比 {(series.spread>0).mean():.1%}")

    # ── 分桶配对价差 ──
    bpair = []
    for col, name in [("size_q", "size"), ("liq_q", "liq")]:
        c2 = merge(cell[(~cell.buy_lim) & cell[col].notna()], ["d", "mod", col])
        for q, gq in c2.groupby(col):
            o = gq[gq["mod"] == 571].set_index("d")["mean_ret"]
            cl = gq[gq["mod"] == 900].set_index("d")["mean_ret"]
            d = (o - cl).dropna()
            if len(d) > 5:
                bpair.append(dict(bucket=name, q=int(q), n=len(d),
                                  spread_bp=round(d.mean() * 1e4, 1),
                                  t=round(d.mean() / (d.std(ddof=1) / np.sqrt(len(d))), 2),
                                  win=round(float((d > 0).mean()), 3)))
    bpair = pd.DataFrame(bpair)
    print("\n=== 分桶配对价差（09:31 − 15:00）===")
    print(bpair.to_string(index=False))

    # ── 分桶 ──
    btabs = {}
    for col, name in [("size_q", "size"), ("liq_q", "liq")]:
        sub = cur_nl.merge(cell[~cell.buy_lim][["d", "mod", col]].drop_duplicates(), on=["d", "mod"])
        # 直接按 cell 分桶重算
        c2 = merge(cell[(~cell.buy_lim) & cell[col].notna()], ["d", "mod", col])
        rr = []
        for q, gq in c2.groupby(col):
            for m in KEYS:
                gg = gq[gq["mod"] == m].set_index("d")["mean_ret"]
                if len(gg):
                    rr.append({"hm": hm(m), "q": int(q),
                               "mean_bp": round(float(gg.mean()) * 1e4, 2),
                               "t": ts(gg)["t"]})
        btabs[name] = pd.DataFrame(rr)
        print(f"\n=== 分{name}五分位 ===")
        print(btabs[name].pivot(index="hm", columns="q", values="mean_bp").to_string())
        btabs[name].to_csv(REP / f"entry_timing_final_by_{name}.csv",
                           index=False, encoding="utf-8-sig")

    # ── 日线外延 2022-2026 ──
    ext = pd.read_parquet(EXT)
    ext["d"] = pd.to_datetime(ext["d"])
    er = []
    for y, g in ext.groupby(ext["d"].dt.year):
        daily = g.groupby("d").agg(ro=("r_open", "mean"), rc=("r_close", "mean"),
                                   ri=("intraday", "mean"))
        dlt = daily["ro"] - daily["rc"]
        er.append(dict(year=int(y), n_days=int(len(daily)),
                       open_bp=round(daily["ro"].mean() * 1e4, 2),
                       open_t=ts(daily["ro"])["t"],
                       open_win=ts(daily["ro"])["win"],
                       close_bp=round(daily["rc"].mean() * 1e4, 2),
                       close_t=ts(daily["rc"])["t"],
                       spread_bp=round(dlt.mean() * 1e4, 2),
                       spread_t=round(dlt.mean() / (dlt.std(ddof=1) / np.sqrt(len(dlt))), 2),
                       intraday_bp=round(daily["ri"].mean() * 1e4, 2)))
    etab = pd.DataFrame(er)
    allday = ext.groupby("d").agg(ro=("r_open", "mean"), rc=("r_close", "mean"))
    dlt = allday["ro"] - allday["rc"]
    etab.loc[len(etab)] = dict(year="ALL", n_days=len(allday),
                               open_bp=round(allday["ro"].mean() * 1e4, 2),
                               open_t=ts(allday["ro"])["t"], open_win=ts(allday["ro"])["win"],
                               close_bp=round(allday["rc"].mean() * 1e4, 2),
                               close_t=ts(allday["rc"])["t"],
                               spread_bp=round(dlt.mean() * 1e4, 2),
                               spread_t=round(dlt.mean() / (dlt.std(ddof=1) / np.sqrt(len(dlt))), 2),
                               intraday_bp=round(allday["ro"].mean() * 1e4 - allday["rc"].mean() * 1e4, 2))
    print("\n=== 日线外延 2022-2026（买在开盘 vs 买在收盘 → 次日开盘卖）===")
    print(etab.to_string(index=False))
    etab.to_csv(REP / "entry_timing_final_daily_ext.csv", index=False, encoding="utf-8-sig")

    out = dict(
        span=[str(cur_nl.d.min().date()), str(cur_nl.d.max().date())],
        n_days=int(cur_nl.d.nunique()),
        n_obs=int(cell.n.sum()),
        cost_bp=COST,
        curve=curve.to_dict("records"),
        by_year=ydf.reset_index().to_dict("records"),
        by_month=mdf.to_dict("records"),
        monthly_spread=ms.to_dict("records"),
        bucket_spread=bpair.to_dict("records"),
        paired=ptab.to_dict("records"),
        by_size=btabs["size"].to_dict("records"),
        by_liq=btabs["liq"].to_dict("records"),
        daily_ext=etab.to_dict("records"),
    )
    OUTJ.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n[wrote] {OUTJ}")


if __name__ == "__main__":
    main()
