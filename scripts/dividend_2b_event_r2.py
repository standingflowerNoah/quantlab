# -*- coding: utf-8 -*-
"""红利 2B 第二轮：预案公告漂移的三个硬闸门验证
==================================================
第一轮发现（dividend_2b_event.py [4]）：剔除财报混发后，预案公告日收盘买入、
持有 20 个交易日，CAR 净均值 +1.322%（n=11504）。本轮逐层验证该发现是否真实可交易：

  [A] 复现 + 市场调整：CAR(0→20) vs 同窗口全市场等权超额（剔除 beta/市场时机）
  [B] 同股对照：事件窗 CAR vs 同股非公告窗 CAR（配对，剔除个股漂移/公告择时）
  [C] 事件聚类：公告高度集中 3-4 月年报季，窗口重叠虚增样本。
      按公告日聚类，聚类层面重算 t 值（有效样本 = 聚类数）
  [D] 可执行组合模拟：公告多为盘后披露 → 买入改为 T+1 收盘（主口径），
      持有 20 个交易日，等权、双边成本 0.2%，逐日净值、分年收益、
      相对全市场等权基准的超额、并发持仓数（容量）。
      附 T 日收盘买入变体（与第一轮口径连续）。

输出: reports/dividend_facts/2b_event_r2_*.csv + 控制台报告
"""
from pathlib import Path
import warnings
import numpy as np
import pandas as pd
import duckdb

warnings.filterwarnings("ignore")
rng = np.random.default_rng(42)
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports/dividend_facts"
OUT.mkdir(exist_ok=True)
con = duckdb.connect()
K = "read_parquet('data/lake/clean/mirror/kline_daily.parquet')"
COST = 0.002          # 往返
HOLD = 20             # 持有交易日
EXCLUDE_BEFORE = 5    # 控制窗排除公告前 5 日（防抢权污染）

# ---------- 数据 ----------
kline = con.execute(
    f"SELECT code, date, close FROM {K} WHERE close>0 AND date>='2022-01-01' "
    "ORDER BY code, date").df()
kline["date"] = pd.to_datetime(kline["date"])
kline = kline.sort_values(["code", "date"]).reset_index(drop=True)
kline["pos"] = kline.groupby("code").cumcount()
kmap = kline.set_index(["code", "pos"])["close"]
pos_of = kline.set_index(["code", "date"])["pos"]
cal_of = kline.set_index(["code", "pos"])["date"]
codes = kline["code"].unique()

# 全市场等权日收益（市场调整与基准共用）
kline["ret"] = kline.groupby("code")["close"].pct_change(fill_method=None)
uni_ret = kline.groupby("date")["ret"].mean()          # 全市场等权

fh = con.execute(
    "SELECT code, announce_date, cash_per10, progress, report_period FROM read_parquet("
    "'data/lake/clean/dividend_announce/part-*.parquet') "
    "WHERE announce_date IS NOT NULL").df()
fh["announce_date"] = pd.to_datetime(fh["announce_date"])
fh = fh[fh["announce_date"] >= "2022-02-01"].drop_duplicates(["code", "announce_date"])

import sys
sys.path.insert(0, ".")
from quantlab.data.store import Store
fin = Store().q("SELECT DISTINCT code, notice_eff FROM finance_history WHERE notice_eff IS NOT NULL")
fin["notice_eff"] = pd.to_datetime(fin["notice_eff"])
fin_set = set(zip(fin["code"], fin["notice_eff"]))
fh["mixed"] = [(c, d) in fin_set or (c, d + pd.Timedelta(days=1)) in fin_set
               or (c, d - pd.Timedelta(days=1)) in fin_set
               for c, d in zip(fh["code"], fh["announce_date"])]
clean = fh[~fh["mixed"]].copy()
print(f"公告事件 {len(fh)} 条（≥2022-02），剔除财报混发后 {len(clean)} 条")
print("progress 分布:", clean["progress"].value_counts().to_dict())

clean["posA"] = [pos_of.get((c, d), -1) for c, d in zip(clean["code"], clean["announce_date"])]
clean = clean[clean["posA"] >= 0].reset_index(drop=True)
print(f"公告日在个股交易日历上的事件 {len(clean)} 条")

def gross(code, p0, p1):
    pb, ps = kmap.get((code, p0)), kmap.get((code, p1))
    if pb is None or ps is None or pb != pb or ps != ps or pb <= 0:
        return np.nan, None, None
    return ps / pb, cal_of.get((code, p0)), cal_of.get((code, p1))

def window_market(code, d0, d1):
    """同窗口全市场等权累计收益"""
    m = uni_ret.loc[(uni_ret.index > d0) & (uni_ret.index <= d1)]
    return (1 + m).prod() - 1

# ---------- [A] 复现 + 市场调整 ----------
rows = []
ev_ret = []          # 每事件 CAR(0→20) 净
ev_excess = []       # 市场调整后
for code, p in zip(clean["code"], clean["posA"]):
    g, d0, d1 = gross(code, p, p + HOLD)
    if g != g:
        ev_ret.append(np.nan); ev_excess.append(np.nan); continue
    r_net = g - 1 - COST
    ev_ret.append(r_net)
    ev_excess.append(g / (1 + window_market(code, d0, d1)) - 1 - COST)
clean["car"] = ev_ret
clean["car_ex"] = ev_excess
clean["buy_date"] = [cal_of.get((c, p)) for c, p in zip(clean["code"], clean["posA"])]
clean["yr"] = clean["announce_date"].dt.year
r = clean["car"].dropna()
e = clean["car_ex"].dropna()
print(f"\n[A] CAR(0→{HOLD}) 复现: n={len(r)}, 净均值 {r.mean()*100:+.3f}%, "
      f"胜率 {(r>0).mean()*100:.1f}%")
print(f"    市场调整后超额: n={len(e)}, 净均值 {e.mean()*100:+.3f}%, "
      f"胜率 {(e>0).mean()*100:.1f}%")

# ---------- [B] 同股对照 ----------
# 每股：事件窗 vs 非公告对照窗（排除自身事件 [-5,+25]），配对比较
ev_by_code = clean.dropna(subset=["car"]).groupby("code")["car"].mean()
last_pos = kline.groupby("code")["pos"].max()
evpos_by_code = clean.groupby("code")["posA"].apply(list)
K_CTRL = 30
ctrl_rows = []
for code, evps in evpos_by_code.items():
    nmax = int(last_pos.get(code, 0))
    banned = np.concatenate([np.arange(p - EXCLUDE_BEFORE, p + HOLD + 6) for p in evps])
    banned = np.unique(banned[(banned >= 0) & (banned <= nmax - HOLD)])
    cand = np.setdiff1d(np.arange(0, nmax - HOLD + 1), banned)
    if len(cand) == 0:
        continue
    pick = rng.choice(cand, size=min(K_CTRL, len(cand)), replace=False)
    rets = [g[0] - 1 - COST for g in (gross(code, int(p), int(p) + HOLD) for p in pick) if g[0] == g[0]]
    if rets:
        ctrl_rows.append({"code": code, "ctrl": np.mean(rets)})
ctrl = pd.DataFrame(ctrl_rows).set_index("code")
pair = pd.DataFrame({"ev": ev_by_code}).join(ctrl).dropna()
diff = pair["ev"] - pair["ctrl"]
t_stat = diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff)))
print(f"\n[B] 同股对照（{len(pair)} 只既有事件又有对照的个股，每股≤{K_CTRL} 个对照窗）")
print(f"    事件窗均值 {pair['ev'].mean()*100:+.3f}% | 对照窗 {pair['ctrl'].mean()*100:+.3f}% "
      f"| 配对差 {diff.mean()*100:+.3f}% | t={t_stat:+.2f} | 个股为正占比 {(diff>0).mean()*100:.0f}%")

# ---------- [C] 事件聚类 ----------
cl = clean.dropna(subset=["car"]).groupby("announce_date")["car"].agg(["mean", "count"])
mu, sd, n = cl["mean"].mean(), cl["mean"].std(ddof=1), len(cl)
print(f"\n[C] 按公告日聚类：{n} 个事件日（有效独立样本≈{n}，事件数 {int(cl['count'].sum())}）")
print(f"    聚类日均 CAR {mu*100:+.3f}% | 日间 SD {sd*100:.3f}% | t={mu/(sd/np.sqrt(n)):+.2f}")
cl_yr = clean.dropna(subset=["car"]).groupby("yr")["car"].agg(["mean", "count"])
cl_yr["n_聚类日"] = clean.dropna(subset=["car"]).groupby("yr")["announce_date"].nunique()
print("\n    分年（事件均值 / 聚类日数）:")
print(cl_yr.round(4).to_string())
big = cl.nlargest(5, "count")
print(f"\n    最拥挤事件日: " + ", ".join(f"{d.date()}({int(c)}起)" for d, c in zip(big.index, big["count"])))

# ---------- [D] 可执行组合模拟 ----------
# 主口径: T+1 收盘买入（公告多为盘后披露），持有 HOLD 个交易日
def simulate(buy_shift, tag):
    acts = []            # (code, buy_pos, sell_pos)
    for code, p in zip(clean["code"], clean["posA"]):
        b, s = p + buy_shift, p + buy_shift + HOLD
        if kmap.get((code, b)) is None or kmap.get((code, s)) is None:
            continue
        acts.append((code, b, s))
    act = pd.DataFrame(acts, columns=["code", "b", "s"])
    act["buy_date"] = [cal_of.get((c, b)) for c, b in zip(act["code"], act["b"])]
    act["sell_date"] = [cal_of.get((c, s)) for c, s in zip(act["code"], act["s"])]
    # 每事件逐日收益序列
    per = {}
    for code, b, s in zip(act["code"], act["b"], act["s"]):
        cl_, ch_ = cal_of.get((code, b)), cal_of.get((code, s))
        for pos in range(b + 1, s + 1):
            d = cal_of.get((code, pos))
            r_ = kmap.get((code, pos)) / kmap.get((code, pos - 1)) - 1
            c_ = 0.001 if pos in (b + 1, s) else 0.0     # 双边各 0.1%
            per.setdefault(d, []).append(r_ - c_)
    days = sorted(per)
    port = pd.Series({d: np.mean(per[d]) for d in days})
    bench = uni_ret.reindex(port.index)
    nav = (1 + port).cumprod()
    navb = (1 + bench).cumprod()
    yr = port.groupby(port.index.year).agg(["sum", "count"])
    yb = bench.groupby(bench.index.year).sum()
    tbl = pd.DataFrame({"策略%": yr["sum"] * 100, "基准%": yb * 100,
                        "超额%": (yr["sum"] - yb) * 100, "活跃日": yr["count"]})
    npos = pd.Series({d: len(per[d]) for d in days})
    vol = port.std(ddof=1) * np.sqrt(250)
    print(f"\n[D] 组合模拟 {tag}: 事件 {len(act)}, 活跃交易日 {len(days)}, "
          f"并发持仓 均值{npos.mean():.0f}/峰值{npos.max()}")
    print(tbl.round(2).to_string())
    print(f"    全期: 策略 {(nav.iloc[-1]-1)*100:+.1f}% | 基准 {(navb.iloc[-1]-1)*100:+.1f}% "
          f"| 年化波动 {vol*100:.1f}% | 日均超额 {(port-bench).mean()*100:+.4f}%")
    return tbl, npos

tbl_b, _ = simulate(1, "主口径 T+1 收盘买入")
tbl_a, _ = simulate(0, "变体 T 收盘买入（第一轮口径）")
tbl_b.to_csv(OUT / "2b_event_r2_port.csv")
tbl_a.to_csv(OUT / "2b_event_r2_port_T0.csv")

# ---------- 汇总 ----------
print("\n========== 二轮结论 ==========")
print(f"[A] 市场调整后超额 {e.mean()*100:+.3f}%（n={len(e)}）")
print(f"[B] 同股配对差 {diff.mean()*100:+.3f}%（t={t_stat:+.2f}，{len(pair)} 只）")
print(f"[C] 聚类后 t={mu/(sd/np.sqrt(n)):+.2f}（有效样本 {n} 个事件日）")
print("    判定标准: 三闸门同向为正且显著 → 通过；否则诚实记负")
clean[["code", "announce_date", "progress", "car", "car_ex", "yr"]].to_csv(
    OUT / "2b_event_r2_events.csv", index=False)
print("DONE")
