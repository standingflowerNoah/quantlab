# -*- coding: utf-8 -*-
"""红利因子研究 · 阶段 3：候选模型对决 + 最终建模

口径：池内 top10 等权，重叠口径净超年化（扣 30bp×网格换手），NW t（lag=h）
区间：IS 2022-12~2024-12 / OOS 2025-01~2026-09 / 全样本

最终模型选择规则（诚实版）：
  1. 先在**预注册模型集**（与既有生产知识同构：M1 amihud、M2 size+amihud）内，用 IS 选持有期
  2. 用 OOS 验证；并同表列出数据挖掘候选（M3/M4/M5）作对照
  3. M4（IS 贪心）作为"纯 IS 选因子"的证伪样本

输出：model_compare.csv / model_summary.csv / final_model_*.csv / final_model.json / curve_final.csv
"""
import json
import numpy as np
import pandas as pd
from pathlib import Path
from dividend_engine import DivData, H_GRID

OUT = Path(__file__).resolve().parent.parent / "reports/dividend_factor"
D = DivData("official")
COST = 0.003
print(f"池内 {len(D.CODES)} 只 | {len(D.DATES)} 交易日 | "
      f"{D.DATES[0].date()} ~ {D.DATES[-1].date()}", flush=True)

# ---------------- 候选模型 ----------------
MODELS = {
    "M1 amihud_20 (单因子)": ["amihud_20"],
    "M2 size+amihud (PROD)": ["size", "amihud_20"],
    "M3 size+amihud+momentum60": ["size", "amihud_20", "momentum_60"],
}
for tag, fn in [("M4 IS贪心", "greedy_is_official.csv"), ("M5 两段一致贪心", "greedy_loose_official.csv")]:
    p = OUT / fn
    if p.exists():
        g = pd.read_csv(p)
        g = g[g["factors"].astype(str).str.len() > 0]
        if len(g):
            i = g["is_net_ann"].idxmax()
            MODELS[f"{tag}(h={int(g.loc[i,'horizon'])})"] = str(g.loc[i, "factors"]).split("|")
print("候选模型：")
for k, v in MODELS.items():
    print(f"  {k}: {v}  (方向 {[D.DIR.get(c,1) for c in v]})")

X = {k: D.mat(v) for k, v in MODELS.items()}

# ---------------- 1. 模型 × 持有期 × 区间 ----------------
rows = []
for nm, x in X.items():
    for h in H_GRID:
        for tag, lim in [("IS", D.IS_LIM), ("OOS", D.OOS_LIM), ("全", D.FULL_LIM)]:
            e = D.evaluate(x, h, 10, COST, lim)
            if e:
                rows.append(dict(model=nm, horizon=h, seg=tag, net_ann=e["net_ann"],
                                 gross_ann=e["gross_ann"], turnover=e["turnover"],
                                 t_nw=e["t_nw"], n=e["n"], win=e["win"]))
cmp = pd.DataFrame(rows)
cmp.to_csv(OUT / "model_compare.csv", index=False)

for tag in ["IS", "OOS", "全"]:
    print(f"\n=== 净超年化%（{tag}） ===")
    print(cmp[cmp.seg == tag].pivot(index="model", columns="horizon", values="net_ann").round(1).to_string())
print("\n=== 毛超年化%（全样本，用于分离成本效应） ===")
print(cmp[cmp.seg == "全"].pivot(index="model", columns="horizon", values="gross_ann").round(1).to_string())


def get(nm, h, seg, col="net_ann"):
    q = cmp[(cmp.model == nm) & (cmp.horizon == h) & (cmp.seg == seg)]
    return float(q[col].iloc[0]) if len(q) else np.nan


summ = []
for nm in MODELS:
    curve = {h: get(nm, h, "IS") for h in H_GRID}
    hIS = max(curve, key=curve.get)
    summ.append(dict(model=nm, best_h_is=hIS, is_net=curve[hIS], oos_at_is_h=get(nm, hIS, "OOS"),
                     full_at_is_h=get(nm, hIS, "全"), is_t=get(nm, hIS, "IS", "t_nw"),
                     oos_t=get(nm, hIS, "OOS", "t_nw"),
                     n_h_both_pos=sum(1 for h in H_GRID
                                      if get(nm, h, "IS") > 0 and get(nm, h, "OOS") > 0),
                     h_detail="|".join(f"h{h}:{get(nm,h,'IS'):+.1f}/{get(nm,h,'OOS'):+.1f}"
                                       for h in H_GRID)))
sm = pd.DataFrame(summ)
print("\n=== IS 选持有期 → OOS 验证 ===")
print(sm[["model", "best_h_is", "is_net", "oos_at_is_h", "full_at_is_h", "is_t", "oos_t",
          "n_h_both_pos"]].round(2).to_string(index=False))
print("\n逐持有期 IS/OOS 净超年化:")
print(sm[["model", "h_detail"]].to_string(index=False))
sm.to_csv(OUT / "model_summary.csv", index=False)

# ---------------- 2. 最终模型 ----------------
FINAL_M = "M2 size+amihud (PROD)"       # 预注册：与既有生产模型同构
curve_is = {h: get(FINAL_M, h, "IS") for h in H_GRID}
FINAL_H = max(curve_is, key=curve_is.get)
print(f"\n=== 最终模型：{FINAL_M}  h={FINAL_H}（IS 选期） ===")
print(f"    IS 净超 {curve_is[FINAL_H]:+.1f}% / OOS 净超 {get(FINAL_M,FINAL_H,'OOS'):+.1f}% "
      f"(t={get(FINAL_M,FINAL_H,'OOS','t_nw'):.2f}) / 全样本 {get(FINAL_M,FINAL_H,'全'):+.1f}%")
XF = X[FINAL_M]

print("\n最终模型持有期曲线（净超年化，扣30bp）:")
hcd = pd.DataFrame([dict(horizon=h,
                         is_net=get(FINAL_M, h, "IS"), oos_net=get(FINAL_M, h, "OOS"),
                         full_net=get(FINAL_M, h, "全"), gross_full=get(FINAL_M, h, "全", "gross_ann"),
                         turnover=get(FINAL_M, h, "全", "turnover"), t_full=get(FINAL_M, h, "全", "t_nw"))
                    for h in H_GRID])
hcd["chosen"] = hcd["horizon"] == FINAL_H
print(hcd.round(2).to_string(index=False))
hcd.to_csv(OUT / "final_model_horizon.csv", index=False)

# 相位闸门
phs = D.multi(XF, 10, FINAL_H, COST)
phd = pd.DataFrame([{k: v for k, v in x.items() if k not in ("net", "bench")} for x in phs])
print(f"\n多相位闸门（h={FINAL_H}, topN=10, 全样本，{len(phd)} 个相位）:")
print(f"  Δ年化 min/中位/max = {phd['ann_ex'].min():.1f} / {phd['ann_ex'].median():.1f} / "
      f"{phd['ann_ex'].max():.1f} → {'全正 ✓ 过闸门二' if (phd['ann_ex'] > 0).all() else '不全正 ✗'}")
print(f"  组合年化 min/中位/max = {phd['ann'].min():.1f} / {phd['ann'].median():.1f} / {phd['ann'].max():.1f}")
print(f"  池基准年化 min/中位/max = {phd['bench_ann'].min():.1f} / {phd['bench_ann'].median():.1f} / "
      f"{phd['bench_ann'].max():.1f}")

# topN 扫描
tn = pd.DataFrame([dict(topn=n,
                        ann=np.mean([y["ann"] for y in D.multi(XF, n, FINAL_H)]),
                        sharpe=np.mean([y["sharpe"] for y in D.multi(XF, n, FINAL_H)]),
                        mdd=np.mean([y["mdd"] for y in D.multi(XF, n, FINAL_H)]),
                        turnover=np.mean([y["turnover"] for y in D.multi(XF, n, FINAL_H)]),
                        ann_ex=np.mean([y["ann_ex"] for y in D.multi(XF, n, FINAL_H)]))
                    for n in [5, 10, 15, 20, 30]])
print("\nTopN 扫描（相位平均，扣30bp）:")
print(tn.round(2).to_string(index=False))
tn.to_csv(OUT / "final_model_topn.csv", index=False)

# 最终统计
final = {f"{FINAL_M}-{n}": D.multi(XF, n, FINAL_H) for n in [10, 20]}
PROD_BENCH = D.multi(D.mat(["amihud_20"]), 10, FINAL_H)  # 仅用于取 bench 序列
bp = PROD_BENCH
fdf = pd.DataFrame([dict(port=nm, ann=np.mean([y["ann"] for y in xs]),
                         sharpe=np.mean([y["sharpe"] for y in xs]),
                         mdd=np.mean([y["mdd"] for y in xs]),
                         turnover=np.mean([y["turnover"] for y in xs]),
                         ann_ex=np.mean([y["ann_ex"] for y in xs]),
                         ex_min=min(y["ann_ex"] for y in xs), ex_max=max(y["ann_ex"] for y in xs))
                    for nm, xs in final.items()])
print("\n最终统计（相位平均，扣30bp）:")
print(fdf.round(2).to_string(index=False))
print(f"池基准年化（各相位）: {np.mean([y['bench_ann'] for y in bp]):.1f}%")
fdf.to_csv(OUT / "final_model_stats.csv", index=False)


def yearly_curve(xs):
    """基于各相位净值曲线取年末值，再算年度收益（正确处理不规则调仓日）"""
    out = []
    for x in xs:
        eq = (1 + x["net"]).cumprod()
        ye = eq.resample("YE").last().dropna()
        prev = eq.iloc[0]
        rs = {}
        for d, v in ye.items():
            rs[d.year] = (v / prev - 1) * 100
            prev = v
        out.append(pd.Series(rs))
    return pd.concat(out, axis=1).mean(axis=1)


yr = pd.DataFrame({nm: yearly_curve(xs) for nm, xs in final.items()})
yr["池基准"] = yearly_curve([dict(net=x["bench"]) for x in bp])
print("\n分年收益%（扣成本，相位平均）:")
print(yr.round(1).to_string())
yr.to_csv(OUT / "final_model_yearly.csv")

# 成本敏感性
cs = pd.DataFrame([dict(cost_bp=c * 1e4,
                        gross=D.evaluate(XF, FINAL_H, 10, c, D.FULL_LIM)["gross_ann"],
                        net=D.evaluate(XF, FINAL_H, 10, c, D.FULL_LIM)["net_ann"],
                        is_net=D.evaluate(XF, FINAL_H, 10, c, D.IS_LIM)["net_ann"],
                        oos_net=D.evaluate(XF, FINAL_H, 10, c, D.OOS_LIM)["net_ann"])
                    for c in [0.0015, 0.003, 0.006]])
print("\n成本敏感性（全样本 / IS / OOS 净超年化）:")
print(cs.round(2).to_string(index=False))
cs.to_csv(OUT / "final_model_cost.csv", index=False)

# 净值曲线（相位平均）：各相位净值按日 ffill 后取平均，避免把重叠持有期收益逐日复利
def equity(xs, key, grid):
    es = []
    for x in xs:
        e = (1 + x[key]).cumprod().reindex(grid).ffill().fillna(1.0)
        es.append(e)
    return pd.concat(es, axis=1).mean(axis=1)


pd.DataFrame({"port": equity(final[f"{FINAL_M}-10"], "net", D.DATES),
              "bench": equity(bp, "bench", D.DATES)}).to_csv(OUT / "curve_final.csv")

json.dump(dict(final_model=FINAL_M, final_h=FINAL_H, factors=MODELS[FINAL_M],
               dirs=[D.DIR.get(c, 1) for c in MODELS[FINAL_M]], topn=10, cost=COST,
               horizon_curve=hcd.to_dict("records"),
               phase_gate_summary=dict(n=len(phd), min=float(phd["ann_ex"].min()),
                                       med=float(phd["ann_ex"].median()),
                                       max=float(phd["ann_ex"].max())),
               stats=fdf.to_dict("records"), yearly=yr.round(2).to_dict(),
               topn_scan=tn.round(2).to_dict("records"),
               model_summary=sm.round(2).to_dict("records")),
          open(OUT / "final_model.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\nDONE")
