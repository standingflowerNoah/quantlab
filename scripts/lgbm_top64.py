#!/usr/bin/env python3
"""GRU 归因对照实验：同 Top64 特征的 LGBM（2026-09-10）
======================================================================
问题：gru_seq 的 OOS IC 0.0911 是基线（280 特征 LGBM）0.0444 的 2 倍，
但 GRU 只用 Top64 特征——提升来自「序列结构」还是「特征选择」？
对照：lgbm_all 同款滚动/purge/超参，仅把特征换成同一份 Top64 列表。
判读：IC(top64-lgbm) ≈ 0.044 → 提升来自序列结构；≈ 0.09 → 来自特征选择。
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
    imp = pd.read_csv(_root / "reports" / "lgbm_all_importance.csv",
                      index_col=0).iloc[:, 0]
    feats = list(imp.sort_values(ascending=False).head(64).index)
    print(f"特征 Top64（与 gru_seq 完全一致）", flush=True)

    feat = load_all_factors(feats)
    keep = [c for c in feats
            if c in feat.columns and feat[c].notna().mean() >= MIN_COV]
    print(f"特征 {len(keep)}/64 达覆盖率阈值", flush=True)
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
    print(f"样本 {len(data)} 行", flush=True)

    import lightgbm as lgb
    all_days = np.sort(data["date"].unique())
    day_pos = {d: i for i, d in enumerate(all_days)}
    start = pd.Timestamp("2024-01-01")
    end = data["date"].max()
    results, n_win = [], 0
    while start < end:
        pred_end = start + pd.DateOffset(months=PREDICT_MONTHS)
        train_start = start - pd.DateOffset(months=TRAIN_MONTHS)
        tr = data[(data["date"] >= train_start) & (data["date"] < start)]
        te = data[(data["date"] >= start) & (data["date"] < pred_end)]
        if len(tr):
            ti = day_pos.get(tr["date"].max())
            if ti is not None:
                cutoff = all_days[max(ti - PURGE_TD, 0)]
                tr = tr[tr["date"] <= cutoff]
        if len(tr) < 100000 or len(te) < 50000:
            start = pred_end
            continue
        n_win += 1
        model = lgb.LGBMRegressor(
            n_estimators=400, learning_rate=0.0421, max_depth=8,
            num_leaves=210, colsample_bytree=0.8879, subsample=0.8789,
            subsample_freq=1, reg_alpha=205.7, reg_lambda=580.98,
            min_child_samples=200, random_state=42, n_jobs=-1, verbosity=-1)
        model.fit(tr[keep], tr["fwd"])
        te = te.copy()
        te["score"] = model.predict(te[keep])
        results.append(te[["date", "code", "score"]])
        print(f"  窗口{n_win}: train {len(tr)} → pred {len(te)}", flush=True)
        start = pred_end

    out = pd.concat(results, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    fwd_full = kl[["date", "code", "fwd"]]
    m = out.merge(fwd_full, on=["date", "code"], how="inner")
    ics = m.groupby("date").apply(
        lambda g: g["score"].rank().corr(g["fwd"].rank()),
        include_groups=False).dropna()
    base = q(f"""
        SELECT CAST(date AS DATE) AS date, code, score
        FROM read_parquet('{str(FACTOR_DIR / "lgbm_all_score").replace(chr(92), "/")}/*.parquet')
    """)
    base["date"] = pd.to_datetime(base["date"])
    mb = base.merge(fwd_full, on=["date", "code"], how="inner")
    ics_b = mb.groupby("date").apply(
        lambda g: g["score"].rank().corr(g["fwd"].rank()),
        include_groups=False).dropna()
    gru = pd.read_parquet(_root / "reports" / "_gru_score.parquet")
    gru["date"] = pd.to_datetime(gru["date"])
    mg = gru.merge(fwd_full, on=["date", "code"], how="inner")
    ics_g = mg.groupby("date").apply(
        lambda g: g["score"].rank().corr(g["fwd"].rank()),
        include_groups=False).dropna()

    print(f"\n=== OOS rank IC（2024-01 起，{len(ics)} 日）===")
    print(f"LGBM_Top64 IC {ics.mean():+.4f} / ICIR {ics.mean()/ics.std():+.3f}")
    print(f"GRU_Top64    IC {ics_g.mean():+.4f} / ICIR {ics_g.mean()/ics_g.std():+.3f}")
    print(f"LGBM_280     IC {ics_b.mean():+.4f} / ICIR {ics_b.mean()/ics_b.std():+.3f}")
    common = ics.index.intersection(ics_g.index)
    d = ics_g.loc[common] - ics.loc[common]
    print(f"GRU−LGBM_Top64 逐日差：均值 {d.mean():+.4f}，胜率 {(d>0).mean():.2f}")

    ics.reset_index().rename(columns={0: "ic"}).to_csv(
        _root / "reports" / "lgbm_top64_oos_ic.csv", index=False)
    out["date"] = out["date"].dt.date
    from quantlab.data.store import Store
    store = None
    for i in range(120):
        try:
            store = Store()
            break
        except Exception:
            if i == 0:
                print("等待 DuckDB 写锁...", flush=True)
            time.sleep(60)
    store.factor_dir("lgbm_top64_score").mkdir(parents=True, exist_ok=True)
    out.to_parquet(str(store.factor_dir("lgbm_top64_score")
                       / "part-b00001.parquet"), index=False)
    print(f"\nscore 已落湖 lgbm_top64_score；完成 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
