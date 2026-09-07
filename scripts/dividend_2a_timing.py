# -*- coding: utf-8 -*-
"""红利 2A 第一轮：红利择时检验（2013-2026，指数层面）
=====================================================
信号（月末，月频）：
  - dy_realized: 过去 12 个月 H00922(全收益) 与 000922(价格) 累计收益差 ≈ 股息率
  - spread:      dy_realized - 十债收益率（股债利差/Fed 模型式）
  - mom:         000922/000300 相对强弱的 12-1 月动量
  - pe_pct:      000922 PE_TTM 的 60 月滚动分位
评估：
  1) 各信号 vs 下月 RS(红利/300) 的 rank IC
  2) 季节性：H00922 vs 000922 分月收益——验证"6 月效应=除息机械"假设（TR 上应消失）
  3) 择时组合：单信号阈值切换（红利 vs 300），0.1% 切换成本；
     对照 = 纯持有红利全收益、50/50 月度再平衡
诚实声明：信号历史仅 ~160 个月且高度相关，第一轮结论只做筛选不做裁决。
"""
from pathlib import Path
import warnings
import numpy as np
import pandas as pd
import duckdb

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports/dividend_facts"
con = duckdb.connect()

# ---- 尝试补拉沪深300全收益 H00300 ----
try:
    h300 = con.execute(
        "SELECT count(*) FROM read_parquet('data/lake/clean/index_kline_ext/part-H00300.parquet')").fetchone()
    print("H00300 已在湖内")
except Exception:
    try:
        import akshare as ak
        df = ak.stock_zh_index_hist_csindex(symbol="H00300", start_date="20130101",
                                            end_date="20260907")
        df = df.rename(columns={"日期": "date", "收盘": "close"})[["date", "close"]]
        df["code"], df["name"] = "H00300", "沪深300全收益"
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df.to_parquet(ROOT / "data/lake/clean/index_kline_ext/part-H00300.parquet", index=False)
        print("H00300 补拉入湖 OK", df.shape)
    except Exception as e:
        print("H00300 补拉失败（退化为价格口径，声明偏差）:", str(e)[:100])

def load_ix(code):
    try:
        df = con.execute(
            f"SELECT date, close, pe_ttm FROM read_parquet('data/lake/clean/index_kline_ext/part-{code}.parquet')").df()
    except Exception:
        df = con.execute(
            f"SELECT date, close, NULL AS pe_ttm FROM read_parquet('data/lake/clean/index_kline_ext/part-{code}.parquet')").df()
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()

p922 = load_ix("000922")            # 价格
tr922 = load_ix("H00922")           # 全收益
tr300 = None
try:
    tr300 = load_ix("H00300")       # 300 全收益（若拉到）
except Exception:
    pass
b300 = con.execute(
    "SELECT date, close FROM read_parquet('data/lake/clean/mirror/index_kline.parquet') "
    "WHERE code='000300.SH'").df()
b300["date"] = pd.to_datetime(b300["date"])
b300 = b300.set_index("date").sort_index()
cgb = con.execute("SELECT date, cgb_10y FROM read_parquet('data/lake/clean/bond_yield/cgb.parquet')").df()
cgb["date"] = pd.to_datetime(cgb["date"])
cgb = cgb.set_index("date").sort_index()["cgb_10y"]

# ---- 月度面板（不整体 dropna：pe_ttm/mirror 回溯短，逐信号取各自样本）----
# 300 腿优先用 H00300 全收益（2013 起），mirror 仅作对照
me_p = p922["close"].resample("ME").last()
me_tr = tr922["close"].resample("ME").last()
me_300 = b300["close"].resample("ME").last()
pe_m = p922["pe_ttm"].resample("ME").last()
panel = pd.DataFrame({"p": me_p, "tr": me_tr, "b300": me_300, "pe": pe_m}).dropna(
    subset=["p", "tr"])
print("pe_ttm 有效起点:", panel["pe"].dropna().index.min())
panel["r_p"] = panel["p"].pct_change()
panel["r_tr"] = panel["tr"].pct_change()
panel["r_300"] = panel["b300"].pct_change()          # mirror 价格（2021 起，对照用）
panel["rs_p"] = (1 + panel["r_p"]) / (1 + panel["r_300"]) - 1
if tr300 is not None:
    me_300tr = tr300["close"].resample("ME").last().reindex(panel.index)
    panel["r_300tr"] = me_300tr.pct_change()
    panel["rs_tr"] = (1 + panel["r_tr"]) / (1 + panel["r_300tr"]) - 1
    rs300_leg = me_300tr                              # 长样本 300 全收益
else:
    rs300_leg = me_300

# ---- 信号 ----
ratio = panel["tr"] / panel["p"]
panel["dy_realized"] = ratio / ratio.shift(12) - 1                     # 滚动 12 月股息率
panel["cgb"] = cgb.resample("ME").last().reindex(panel.index)
print("cgb_10y 量级示例:", panel["cgb"].dropna().iloc[-1], "(百分点口径则除以100)")
if panel["cgb"].dropna().median() > 1:      # 百分点 → 小数
    panel["cgb"] = panel["cgb"] / 100
panel["spread"] = panel["dy_realized"] - panel["cgb"]
rs_m = panel["p"] / rs300_leg                                          # 价格/全收益 混合 RS：成分恒定，动量方向一致
panel["mom"] = rs_m / rs_m.shift(12) - rs_m.shift(1) / rs_m.shift(1)   # 12-1 月 RS 动量
panel["pe_pct"] = panel["pe"].rolling(60, min_periods=36).rank(pct=True)

valid = panel.dropna(subset=["spread"])
print(f"spread 样本: {valid.index.min().date()} ~ {panel.index.max().date()}  {len(valid)} 个月")
valid_mom = panel.dropna(subset=["mom"])
print(f"mom 样本: {valid_mom.index.min().date()} 起  {len(valid_mom)} 个月")

# ---- 1) 信号 IC ----
print("\n[1] 信号 vs 下月 RS rank IC（价格口径 / 全收益口径）")
sig_cols = ["spread", "mom", "pe_pct", "dy_realized"]
rows = []
for c in sig_cols:
    s = panel[c]
    for tname, tcol in [("RS价格", "rs_p"), ("RS全收益", "rs_tr")]:
        if tcol not in panel or panel[tcol].notna().sum() < 24:
            continue
        fwd = panel[tcol].shift(-1)
        ok = s.notna() & fwd.notna()
        if ok.sum() < 36:
            continue
        ic = s[ok].rank().corr(fwd[ok].rank())
        sub = pd.DataFrame({"s": s[ok], "f": fwd[ok]})
        ic_by_year = sub.groupby(sub.index.year).apply(
            lambda g: g["s"].rank().corr(g["f"].rank()))
        rows.append({"signal": c, "target": tname, "IC": round(ic, 3), "n": int(ok.sum()),
                     "IC>0年份占比": f"{(ic_by_year > 0).mean():.0%}",
                     "起点": str(ok[ok].index.min().date())})
ic_df = pd.DataFrame(rows)
print(ic_df.to_string(index=False))
ic_df.to_csv(OUT / "2a_signal_ic.csv", index=False)

# ---- 2) 季节性：6 月效应证伪 ----
print("\n[2] 分月均收益%（价格指数 vs 全收益指数）——除息机械效应检验")
seas = pd.DataFrame({
    "000922价格": panel["r_p"].groupby(panel.index.month).mean() * 100,
    "H00922全收益": panel["r_tr"].groupby(panel.index.month).mean() * 100,
    "000300价格": panel["r_300"].groupby(panel.index.month).mean() * 100,
})
if "r_300tr" in panel and panel["r_300tr"].notna().sum() > 24:
    seas["H00300全收益"] = panel["r_300tr"].groupby(panel.index.month).mean() * 100
print(seas.round(2).to_string())
seas.to_csv(OUT / "2a_seasonality.csv")
jun = seas.loc[6]
if "H00300全收益" in seas:
    print(f"  6月: 红利价格 {jun['000922价格']:.2f}% vs 全收益 {jun['H00922全收益']:.2f}% "
          f"（机械股息≈{jun['H00922全收益']-jun['000922价格']:.2f}%）；"
          f"300: {jun['000300价格']:.2f}% vs {jun['H00300全收益']:.2f}%")
# 分年 5/6/7 月 TR 收益（除息机械效应是否解释"6月弱"）
may_jul = panel[panel.index.month.isin([5, 6, 7])][["r_tr", "r_300"]].copy()
if "r_300tr" in panel:
    may_jul["r_300tr"] = panel["r_300tr"]
pivot = may_jul.groupby([may_jul.index.year, may_jul.index.month]).mean().unstack() * 100
print("\n  5/6/7 月分年收益%（上=红利TR 下=300）：")
print(pivot.round(1).to_string())
pivot.to_csv(OUT / "2a_mayjul_by_year.csv")

# ---- 3) 择时组合（各信号从其有效起点评估，对照基准同窗对齐）----
print("\n[3] 择时组合（月频，红利TR vs 300TR 切换，0.1% 换仓成本）")

def timing(sig, mode="above_median", warmup=24):
    s = sig.copy()
    if mode == "above_median":
        pos = (s > s.expanding(min_periods=warmup).median()).astype(float)
    elif mode == "above_zero":
        pos = (s > 0).astype(float)
    elif mode == "below":
        pos = (s < s.expanding(min_periods=warmup).median()).astype(float)
    pos = pos.shift(1).fillna(0.0)                     # 月末信号 → 下月持有
    gross = pos * panel["r_tr"] + (1 - pos) * panel["r_300tr"]
    turn = pos.diff().abs().fillna(0)
    return gross - turn * 0.001, pos

def stats(r):
    r = r.dropna()
    if len(r) < 12:
        return (np.nan, np.nan, np.nan, len(r))
    ann = (1 + r).prod() ** (12 / len(r)) - 1
    shp = r.mean() / r.std() * np.sqrt(12) if r.std() > 0 else np.nan
    dd = ((1 + r).cumprod() / (1 + r).cumprod().cummax() - 1).min()
    return ann * 100, shp, dd * 100, len(r)

cases = [("spread>中位", panel["spread"], "above_median"),
         ("spread>0", panel["spread"], "above_zero"),
         ("mom12-1>中位", panel["mom"], "above_median"),
         ("mom12-1>0", panel["mom"], "above_zero"),
         ("pe_pct<中位(低估值持红利)", panel["pe_pct"], "below")]
rows = []
for nm, sig, mode in cases:
    base = sig.dropna().index.min()
    r_t, pos = timing(sig, mode)
    # 同窗对照：纯持红利 / 50/50
    win = panel.loc[base:]
    r_bh = win["r_tr"]
    half = pd.concat([win["r_tr"], win["r_300tr"]], axis=1).mean(axis=1)
    a1, s1, d1, n = stats(r_t)
    a2, s2, d2, _ = stats(r_bh)
    a3, s3, d3, _ = stats(half)
    rows.append({"择时": nm, "起点": str(base.date()), "月数": n,
                 "择时年化%": round(a1, 1), "择时夏普": round(s1, 2), "择时回撤%": round(d1, 1),
                 "持红利年化%": round(a2, 1), "持红利夏普": round(s2, 2),
                 "50/50年化%": round(a3, 1), "50/50夏普": round(s3, 2),
                 "持红利占比": f"{(pos.loc[base:] > 0.5).mean():.0%}",
                 "持红利时跑赢占比": f"{(win['r_tr'][pos.loc[base:] > 0.5] > win['r_300tr'][pos.loc[base:] > 0.5]).mean():.0%}"})
t_df = pd.DataFrame(rows)
print(t_df.to_string(index=False))
t_df.to_csv(OUT / "2a_timing.csv", index=False)
print("DONE")
