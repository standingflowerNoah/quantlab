# -*- coding: utf-8 -*-
"""红利因子研究 · 阶段 2：持有期选择 + 因子组合 + 建模回测（矩阵引擎版）

评估引擎：
  - 毛超额：**重叠口径**——每个交易日按池内 rank 组合建仓，取 h 日含息超额（组合 − 池内等权），
    ~850 个观测（与 h 无关），t 用 Newey-West(lag=h) 校正重叠自相关
  - 换手：**实际 h 日网格**（相位 0..h-1 平均）的 Jaccard 换手
  - 净超年化 = (期均毛超额 − 换手 × 往返成本) × 250/h   → 各持有期同等功效可比
  - 最终展示另给非重叠单相位网格回测（可交易口径）与相位离散度

选择模式：
  is      仅用 IS(<2025-01) 选因子与持有期 → OOS(>=2025-01) 严格样本外验证
  stable  IS 与 OOS 同向且各自 |t|>1.5（两段一致性）
  loose   IS |t|>1.0 且 OOS |t|>1.5 同向

用法: python scripts/dividend_factor_combo.py [official|approx] [is|stable|loose]
"""
import sys
import json
import numpy as np
import pandas as pd
import duckdb
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports/dividend_factor"
POOL = sys.argv[1] if len(sys.argv) > 1 else "official"
MODE = sys.argv[2] if len(sys.argv) > 2 else "is"
H_GRID = [5, 10, 20, 40, 60]
OOS_START = pd.Timestamp("2025-01-01")
COST = 0.003
COST_SCEN = [0.0015, 0.003, 0.006]
TOPNS = [5, 10, 15, 20]
ANN = 250.0

panel = pd.read_parquet(OUT / "panel.parquet")
pool = pd.read_parquet(OUT / f"pool_{POOL}.parquet")
base = panel.merge(pool, on=["date", "code"], how="inner")
base = base[base["date"] >= pd.Timestamp("2022-06-01")].copy()
screen = pd.read_csv(OUT / f"factor_screen_{POOL}.csv")
DATES = pd.DatetimeIndex(sorted(base["date"].unique()))
CODES = np.array(sorted(base["code"].unique()))
IS_MASK = np.asarray(DATES < OOS_START)
print(f"[{POOL}/{MODE}] 池内 {len(base):,} 行 | {len(DATES)} 交易日 | {len(CODES)} 只 | "
      f"{DATES[0].date()} ~ {DATES[-1].date()}", flush=True)

# ---------------- A. 候选因子（方向只用 IS 判定） ----------------
full = screen[(screen["coverage"] > 0.8) & (screen["n_dates"] > 100)].copy()
full["side"] = np.where(full["head_bp"] >= full["tail_bp"], 1, -1)
full["gain"] = np.maximum(full["head_bp"], full["tail_bp"])
DIR = {f: (1 if g["side"].sum() >= 0 else -1)
       for f, g in full[full["segment"] == "is"].groupby("factor")}
is_s = full[full["segment"] == "is"].set_index(["factor", "horizon"])
oos_s = full[full["segment"] == "oos"].set_index(["factor", "horizon"])
key = is_s.index.intersection(oos_s.index)
A = is_s.loc[key, ["head_t_nw", "gain"]].rename(columns={"head_t_nw": "t_is", "gain": "gain_is"})
J = A.join(oos_s.loc[key, ["head_t_nw"]].rename(columns={"head_t_nw": "t_oos"})).reset_index()
if MODE == "is":
    sel = J[(J["t_is"].abs() > 1.5) & (J["gain_is"] > 0)]
elif MODE == "stable":
    sel = J[(J["t_is"].abs() > 1.5) & (J["t_oos"].abs() > 1.5)
            & (np.sign(J["t_is"]) == np.sign(J["t_oos"])) & (J["gain_is"] > 0)]
else:
    sel = J[(J["t_is"].abs() > 1.0) & (J["t_oos"].abs() > 1.5)
            & (np.sign(J["t_is"]) == np.sign(J["t_oos"])) & (J["gain_is"] > 0)]
print(f"候选记录 {len(sel)}（{MODE}）")
cand_per_h = {h: list(sel[sel["horizon"] == h].nlargest(10, "gain_is")["factor"]) for h in H_GRID}
uni = sorted(set().union(*cand_per_h.values()) | {"size", "amihud_20"})
print(f"候选因子并集 {len(uni)}：{uni}")
print("方向(IS):", {k: DIR.get(k, 1) for k in uni}, flush=True)

# ---------------- B. 矩阵化：因子 rank 与前瞻收益 ----------------
RETM = {}
for h in H_GRID:
    p = base.pivot(index="date", columns="code", values=f"tot_ret_{h}")
    RETM[h] = p.reindex(index=DATES, columns=CODES).values
    if h == H_GRID[0]:
        print(f"收益矩阵 {RETM[h].shape}", flush=True)

con = duckdb.connect()
code_sql = ",".join(f"'{c}'" for c in CODES)
FM = {}
for f in uni:
    fac = con.execute(
        f"SELECT date, code, value FROM read_parquet("
        f"'{str(ROOT / f'data/lake/factor/{f}').replace(chr(92), '/')}/part-*.parquet') "
        f"WHERE date >= DATE '2022-06-01' AND code IN ({code_sql})").df()
    fac["date"] = pd.to_datetime(fac["date"])
    d = base[["date", "code"]].merge(fac, on=["date", "code"], how="left")
    d["r"] = d.groupby("date")["value"].rank(pct=True) * float(DIR.get(f, 1))
    FM[f] = d.pivot(index="date", columns="code", values="r").reindex(index=DATES, columns=CODES).values
print(f"因子矩阵 {len(uni)} × {RETM[5].shape}", flush=True)
CORR = pd.DataFrame(
    {a: {b: np.corrcoef(FM[a][~np.isnan(FM[a]) & ~np.isnan(FM[b])],
                        FM[b][~np.isnan(FM[a]) & ~np.isnan(FM[b])])[0, 1] for b in uni} for a in uni})


def nw_t(x, lag):
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 20:
        return np.nan
    d = x - x.mean()
    s = (d ** 2).mean()
    for l in range(1, min(lag, n - 1) + 1):
        s += 2 * (1 - l / (lag + 1)) * (d[l:] * d[:-l]).mean()
    return x.mean() / np.sqrt(s / n) if s > 0 else np.nan


def topn_idx(x, r, topn):
    m = ~(np.isnan(x) | np.isnan(r))
    if m.sum() < max(topn, 30):
        return None
    idx = np.where(m)[0]
    return idx, idx[np.argsort(-x[idx], kind="stable")][:topn]


def gross_ex(X, h, topn=10, lim=None):
    R = RETM[h]
    out = np.full(len(DATES), np.nan)
    for i in range(len(DATES)):
        if lim is not None and not (lim[0] <= DATES[i] <= lim[1]):
            continue
        t = topn_idx(X[i], R[i], topn)
        if t is None:
            continue
        idx, top = t
        out[i] = R[i][top].mean() - R[i][idx].mean()
    return pd.Series(out, index=DATES).dropna()


def turnover_grid(X, h, topn=10):
    R, tos = RETM[h], []
    for ph in range(h):
        sets = []
        for i in range(ph, len(DATES), h):
            t = topn_idx(X[i], R[i], topn)
            if t is None:
                continue
            sets.append(set(CODES[t[1]]))
        if len(sets) > 1:
            tos.append(np.mean([1 - len(a & b) / len(a | b) for a, b in zip(sets[:-1], sets[1:])]))
    return np.mean(tos) if tos else np.nan


def evaluate(X, h, topn=10, cost=COST, lim=None):
    ex = gross_ex(X, h, topn, lim)
    if len(ex) < 30:
        return None
    to = turnover_grid(X, h, topn)
    g = ex.mean() * ANN / h * 100
    return dict(n=len(ex), gross_ann=g, net_ann=g - to * cost * ANN / h * 100,
                turnover=to, t_nw=nw_t(ex.values, h), win=(ex > 0).mean(), ex=ex)


def ann_stats(r):
    if r is None or len(r) < 6:
        return (np.nan,) * 3
    span = (r.index[-1] - r.index[0]).days / 365.25
    ppy = len(r) / span if span > 0 else np.nan
    cum = (1 + r).cumprod()
    return ((1 + r).prod() ** (ppy / len(r)) - 1) * 100, \
           (r.mean() / r.std() * np.sqrt(ppy) if r.std() else np.nan), \
           (cum / cum.cummax() - 1).min() * 100


BENCH = {}
for h in H_GRID:
    with np.errstate(invalid="ignore"):
        BENCH[h] = pd.Series(np.nanmean(RETM[h], axis=1), index=DATES).dropna()


def simulate_grid(X, h, phase, topn=10, cost=COST):
    R = RETM[h]
    dates, rets, sets = [], [], []
    for i in range(phase, len(DATES), h):
        t = topn_idx(X[i], R[i], topn)
        if t is None:
            continue
        dates.append(DATES[i])
        rets.append(R[i][t[1]].mean())
        sets.append(set(CODES[t[1]]))
    if len(rets) < 6:
        return None
    r = pd.Series(rets, index=pd.DatetimeIndex(dates))
    to = (np.mean([1 - len(a & b) / len(a | b) for a, b in zip(sets[:-1], sets[1:])])
          if len(sets) > 1 else np.nan)
    net = r - to * cost
    b = BENCH[h].reindex(r.index)
    a, s, d = ann_stats(net)
    return dict(phase=phase, n=len(r), ann=a, sharpe=s, mdd=d, bench_ann=ann_stats(b)[0],
                ann_ex=ann_stats(net - b)[0], turnover=to, net=net, bench=b)


def mat(cols):
    return np.nanmean(np.stack([FM[c] for c in cols]), axis=0)


IS_LIM = (DATES[0], pd.Timestamp("2024-12-31"))
OOS_LIM = (pd.Timestamp("2025-01-01"), DATES[-1])
FULL_LIM = (DATES[0], DATES[-1])

# ---------------- C. 单因子 × 持有期（IS） ----------------
print("\n=== C. 单因子 × 持有期：IS 净超年化%（topN=10，扣30bp×网格换手） ===")
rows = []
for f in uni:
    for h in H_GRID:
        e = evaluate(FM[f], h, lim=IS_LIM)
        if e:
            rows.append(dict(factor=f, horizon=h, **{k: v for k, v in e.items() if k != "ex"}))
sw = pd.DataFrame(rows)
sw.to_csv(OUT / f"single_sweep_{MODE}_{POOL}.csv", index=False)
print(sw.pivot(index="factor", columns="horizon", values="net_ann").round(1).to_string())
print("\n各持有期汇总（IS 净超年化）:")
print(sw.groupby("horizon")["net_ann"].agg(["mean", "max", "count"]).round(1).to_string())


# ---------------- D. 贪心（IS） ----------------
def greedy(h, topn=10, pool_f=None, max_k=4, min_gain=0.5, corr_max=0.85, lim=IS_LIM):
    chosen, best = [], -1e9
    remain = list(pool_f)
    while len(chosen) < max_k and remain:
        cands = []
        for c in remain:
            if any(abs(CORR.loc[c, x]) > corr_max for x in chosen):
                continue
            e = evaluate(mat(chosen + [c]), h, topn, lim=lim)
            if e:
                cands.append((e["net_ann"], c))
        if not cands:
            break
        cands.sort(reverse=True)
        if cands[0][0] <= best + min_gain:
            break
        best, add = cands[0]
        chosen.append(add)
        remain.remove(add)
    return chosen, best


print("\n=== D. 组合贪心（IS，topN=10） ===")
combo_res = {}
for h in H_GRID:
    ch, b = greedy(h, 10, sorted(set(cand_per_h[h]) | {"amihud_20", "size"}))
    combo_res[h] = (ch, b)
    print(f" h={h:2d}: {ch} → IS 净超年化 {b:+.1f}%")
pd.DataFrame([dict(horizon=h, factors="|".join(v[0]), is_net_ann=v[1])
              for h, v in combo_res.items()]).to_csv(OUT / f"greedy_{MODE}_{POOL}.csv", index=False)

# ---------------- E. 赢家 ----------------
best_h = max(combo_res, key=lambda k: combo_res[k][1])
cols = combo_res[best_h][0]
print(f"\n=== E. 赢家 h={best_h} 因子={cols} ===")
if not cols:
    raise SystemExit("无可用组合")
XW = mat(cols)

print("\n--- 分段（重叠口径，topN=10） ---")
seg = []
for tag, lim in [("IS", IS_LIM), ("OOS", OOS_LIM), ("全样本", FULL_LIM)]:
    e = evaluate(XW, best_h, 10, lim=lim)
    if e:
        seg.append(dict(seg=tag, n=e["n"], gross_ann=round(e["gross_ann"], 2),
                        net_ann=round(e["net_ann"], 2), turnover=round(e["turnover"], 3),
                        t_nw=round(e["t_nw"], 2), win=round(e["win"], 3)))
segd = pd.DataFrame(seg)
print(segd.to_string(index=False))
segd.to_csv(OUT / f"winner_segments_{MODE}_{POOL}.csv", index=False)

print(f"\n--- 多相位闸门（h={best_h}, topN=10, 全样本，非重叠网格） ---")
ph_rows = [x for x in (simulate_grid(XW, best_h, ph, 10) for ph in range(best_h)) if x]
phd = pd.DataFrame([{k: v for k, v in x.items() if k not in ("net", "bench")} for x in ph_rows])
print(phd.round(2).to_string(index=False))
print(f"  → Δ年化 {list(phd['ann_ex'].round(1))} "
      f"{'全正 ✓ 过闸门二' if (phd['ann_ex'] > 0).all() else '不全正 ✗'}")
phd.to_csv(OUT / f"winner_phases_{MODE}_{POOL}.csv", index=False)

# ---------------- F. 最终回测 ----------------
print(f"\n=== F. 最终回测（h={best_h}, 相位平均, 扣30bp×换手） ===")


def multi(X, n, h=None, cost=COST):
    h = h or best_h
    return [x for x in (simulate_grid(X, h, ph, n, cost) for ph in range(h)) if x]


PROD_X = 0.5 * FM["size"] + 0.5 * FM["amihud_20"]   # FM 已含方向
cands = {f"红利增强-{n}": multi(XW, n) for n in TOPNS}
cands.update({f"池内PROD-{n}": multi(PROD_X, n) for n in [10, 20]})
bphs = [x for x in (simulate_grid(PROD_X, best_h, ph, 5) for ph in range(best_h)) if x]
bench_ann = np.mean([x["bench_ann"] for x in bphs if x["bench_ann"] == x["bench_ann"]])

print(f"{'组合':<16}{'年化%':>8}{'夏普':>7}{'回撤%':>8}{'换手%':>8}{'超额%':>9}{'相位Δ区间':>16}")
print(f"{'池基准(等权)':<16}{bench_ann:>8.1f}{'—':>7}{'—':>8}{'—':>8}{'—':>9}{'—':>16}")
res = []
for nm, xs in cands.items():
    if not xs:
        continue
    exs = [x["ann_ex"] for x in xs]
    ann = np.mean([x["ann"] for x in xs])
    sh = np.mean([x["sharpe"] for x in xs])
    mdd = np.mean([x["mdd"] for x in xs])
    to = np.mean([x["turnover"] for x in xs])
    print(f"{nm:<16}{ann:>8.1f}{sh:>7.2f}{mdd:>8.1f}{to*100:>8.1f}{np.mean(exs):>9.1f}"
          f"{f'{min(exs):.1f}~{max(exs):.1f}':>16}")
    res.append(dict(port=nm, ann=ann, sharpe=sh, mdd=mdd, turnover=to, ann_ex=np.mean(exs),
                    ex_min=min(exs), ex_max=max(exs)))
pd.DataFrame(res).to_csv(OUT / f"final_ports_{MODE}_{POOL}.csv", index=False)


def yearly(xs):
    df = pd.concat([pd.Series({y: ((1 + x["net"][x["net"].index.year == y]).prod() - 1) * 100
                               for y in sorted(set(x["net"].index.year))}) for x in xs], axis=1)
    return df.mean(axis=1)


yr = pd.DataFrame({nm: yearly(xs) for nm, xs in cands.items() if xs})
print("\n分年收益%（扣成本，相位平均）:")
print(yr.round(1).to_string())
yr.to_csv(OUT / f"final_yearly_{MODE}_{POOL}.csv")

print(f"\n成本敏感性（红利增强-10, h={best_h}, 重叠口径）:")
for c in COST_SCEN:
    e = evaluate(XW, best_h, 10, cost=c, lim=FULL_LIM)
    print(f"  往返 {c*1e4:.0f}bp: 毛超年化 {e['gross_ann']:.1f}%  净超年化 {e['net_ann']:+.1f}%"
          f"  （换手 {e['turnover']*100:.1f}%）")

# 落盘净值序列（供报告绘图）
pd.concat([x["net"] for x in cands["红利增强-10"]], axis=1).to_csv(
    OUT / f"curve_{MODE}_{POOL}.csv")
json.dump(dict(pool=POOL, mode=MODE, horizon=best_h, factors=cols, topn=10, cost=COST,
               dir={k: DIR.get(k, 1) for k in cols}, segments=seg,
               phase_ann_ex=[round(x, 2) for x in phd["ann_ex"]]),
          open(OUT / f"final_config_{MODE}_{POOL}.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print("\nDONE")
