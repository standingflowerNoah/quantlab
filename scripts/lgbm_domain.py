#!/usr/bin/env python3
"""Phase 4 · LightGBM 风格分域训练（兴证《基于风格因子的非线性分域训练》思路适配）
======================================================================
与 lgbm_all.py（全池单模型）的唯一差异：按 size 截面三分位把全市场切成
小/中/大市值三个域，每域独立训练 LGBM，预测也在域内完成——让模型在域内
学习非线性关系，不再被跨域的规模效应主导。

复用 lgbm_all 的全部防泄露纪律：T+1→T+21 标签、rank pct、24m 滚动 +
6m 重训练、purge 21 交易日、qlib 官方超参。
评估：OOS rank IC vs lgbm_all_score 基线（同标签逐日差）。
输出：factor/lgbm_domain_score 湖 + reports/lgbm_domain_oos_ic.csv
"""
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root / "scripts"))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from lgbm_all import (FACTOR_DIR, HORIZON, MIN_COV, MIRROR, PREDICT_MONTHS,
                      PURGE_TD, TRAIN_MONTHS, load_all_factors, q)


def main():
    t0 = time.time()
    factor_names = sorted(p.name for p in FACTOR_DIR.iterdir()
                          if p.is_dir() and not p.name.startswith("_"))
    print(f"因子库 {len(factor_names)} 个，构建特征矩阵...", flush=True)
    feat = load_all_factors(factor_names)
    cov = feat.notna().mean()
    keep = cov[cov >= MIN_COV].index.tolist()
    print(f"特征矩阵 {feat.shape}，覆盖率≥{MIN_COV:.0%} 保留 {len(keep)} 个",
          flush=True)
    feat = feat[keep].astype("float32").fillna(0.5).reset_index()
    feat["date"] = pd.to_datetime(feat["date"])

    kl = q(f"""
        SELECT CAST(date AS DATE) AS date, code,
               LEAD(close * adj_factor, 1) OVER p AS c1,
               LEAD(close * adj_factor, {HORIZON + 1}) OVER p AS c{HORIZON+1}
        FROM read_parquet('{str(MIRROR).replace(chr(92), "/")}')
        WINDOW p AS (PARTITION BY code ORDER BY date)
    """)
    kl["fwd"] = kl[f"c{HORIZON+1}"] / kl["c1"] - 1
    kl = kl.dropna(subset=["fwd"])
    kl["fwd"] = kl.groupby("date")["fwd"].rank(pct=True)
    kl["date"] = pd.to_datetime(kl["date"])
    data = feat.merge(kl[["date", "code", "fwd"]], on=["date", "code"], how="inner")
    print(f"样本 {len(data)} 行，{data['date'].min().date()} ~ "
          f"{data['date'].max().date()}", flush=True)

    # 域标签：size 截面三分位（库内 size 因子，rank pct）
    size_rank = q(f"""
        SELECT CAST(date AS DATE) AS date, code,
               rank() OVER (PARTITION BY CAST(date AS DATE) ORDER BY value) * 1.0
               / NULLIF(COUNT(value) OVER (PARTITION BY CAST(date AS DATE)), 0)
               AS rk
        FROM read_parquet('{str(FACTOR_DIR / "size").replace(chr(92), "/")}/*.parquet')
        WHERE value IS NOT NULL AND isfinite(value)
    """)
    size_rank["date"] = pd.to_datetime(size_rank["date"])
    size_rank["domain"] = pd.cut(size_rank["rk"], [0, 1/3, 2/3, 1.0],
                                 labels=["large", "mid", "small"]).astype(str)
    data = data.merge(size_rank[["date", "code", "domain"]],
                      on=["date", "code"], how="left")
    data["domain"] = data["domain"].fillna("mid")
    print("域分布：", data.groupby("domain")["code"].count().to_dict(), flush=True)

    import lightgbm as lgb
    all_days = np.sort(data["date"].unique())
    day_pos = {d: i for i, d in enumerate(all_days)}
    start = pd.Timestamp("2024-01-01")
    end = data["date"].max()
    results, n_win = [], 0
    while start < end:
        pred_end = start + pd.DateOffset(months=PREDICT_MONTHS)
        train_start = start - pd.DateOffset(months=TRAIN_MONTHS)
        tr_all = data[(data["date"] >= train_start) & (data["date"] < start)]
        te_all = data[(data["date"] >= start) & (data["date"] < pred_end)]
        if len(tr_all):
            ti = day_pos.get(tr_all["date"].max())
            if ti is not None:
                cutoff = all_days[max(ti - PURGE_TD, 0)]
                tr_all = tr_all[tr_all["date"] <= cutoff]
        if len(tr_all) < 100000 or len(te_all) < 50000:
            print(f"  窗口 {start.date()}: 样本不足跳过", flush=True)
            start = pred_end
            continue
        n_win += 1
        te_parts = []
        for dom in ("small", "mid", "large"):
            tr = tr_all[tr_all["domain"] == dom]
            te = te_all[te_all["domain"] == dom]
            if len(tr) < 30000 or len(te) < 5000:
                print(f"  窗口{n_win} {dom}: 样本不足"
                      f"（{len(tr)}/{len(te)}）并入全域", flush=True)
                continue
            model = lgb.LGBMRegressor(
                n_estimators=400, learning_rate=0.0421, max_depth=8,
                num_leaves=210, colsample_bytree=0.8879, subsample=0.8789,
                subsample_freq=1, reg_alpha=205.7, reg_lambda=580.98,
                min_child_samples=200, random_state=42, n_jobs=-1, verbosity=-1)
            model.fit(tr[keep], tr["fwd"])
            te = te.copy()
            te["score"] = model.predict(te[keep])
            te_parts.append(te[["date", "code", "domain", "score"]])
            print(f"  窗口{n_win} {dom}: train {len(tr)} → pred {len(te)}",
                  flush=True)
        # 域内样本不足的部分用全域样本补训一个兜底模型
        miss = te_all[~te_all["code"].isin(
            pd.concat(te_parts)["code"])] if te_parts else te_all
        if len(miss) > 0:
            model = lgb.LGBMRegressor(
                n_estimators=400, learning_rate=0.0421, max_depth=8,
                num_leaves=210, colsample_bytree=0.8879, subsample=0.8789,
                subsample_freq=1, reg_alpha=205.7, reg_lambda=580.98,
                min_child_samples=200, random_state=42, n_jobs=-1, verbosity=-1)
            model.fit(tr_all[keep], tr_all["fwd"])
            miss = miss.copy()
            miss["score"] = model.predict(miss[keep])
            te_parts.append(miss[["date", "code", "domain", "score"]])
        results.append(pd.concat(te_parts, ignore_index=True))
        start = pred_end

    if not results:
        raise RuntimeError("无有效预测窗口")
    out = pd.concat(results, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])

    fwd_full = kl[["date", "code", "fwd"]]
    m = out.merge(fwd_full, on=["date", "code"], how="inner")
    ics = m.groupby("date").apply(
        lambda g: g["score"].rank().corr(g["fwd"].rank()),
        include_groups=False).dropna()

    # 基线对比：lgbm_all_score 同标签逐日 IC
    base = q(f"""
        SELECT CAST(date AS DATE) AS date, code, score
        FROM read_parquet('{str(FACTOR_DIR / "lgbm_all_score").replace(chr(92), "/")}/*.parquet')
    """)
    base["date"] = pd.to_datetime(base["date"])
    mb = base.merge(fwd_full, on=["date", "code"], how="inner")
    ics_b = mb.groupby("date").apply(
        lambda g: g["score"].rank().corr(g["fwd"].rank()),
        include_groups=False).dropna()

    common = ics.index.intersection(ics_b.index)
    d = ics.loc[common] - ics_b.loc[common]
    print(f"\n=== OOS rank IC（2024-01 起，{len(ics)} 日）===")
    print(f"分域  IC {ics.mean():+.4f} / ICIR {ics.mean()/ics.std():+.3f} / "
          f"win {(ics>0).mean():.2f}")
    print(f"基线  IC {ics_b.mean():+.4f} / ICIR {ics_b.mean()/ics_b.std():+.3f}")
    print(f"逐日差（分域−基线）：均值 {d.mean():+.4f}，胜率 {(d>0).mean():.2f}"
          f"（{len(common)} 共同日）")
    ics_y = pd.DataFrame({"ic": ics})
    ics_y["year"] = pd.to_datetime(ics_y.index).year
    print(ics_y.groupby("year")["ic"].agg(["mean", "count"]).round(4).to_string())

    from quantlab.data.store import Store
    store = Store()
    out["date"] = out["date"].dt.date
    store.factor_dir("lgbm_domain_score").mkdir(parents=True, exist_ok=True)
    out.to_parquet(str(store.factor_dir("lgbm_domain_score")
                       / "part-b00001.parquet"), index=False)
    ics.reset_index().rename(columns={0: "ic"}).to_csv(
        _root / "reports" / "lgbm_domain_oos_ic.csv", index=False)
    print(f"\nscore 已落湖 lgbm_domain_score：{len(out)} 行；完成 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
