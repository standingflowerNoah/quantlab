#!/usr/bin/env python3
"""Phase 4 · GRU 时序模型（兴证 MEGA-GRU 思路适配版，2026-09-10）
======================================================================
输入：Top-K 因子（lgbm_all_importance 前 64）的逐日 rank pct 序列（20 日窗口）
模型：GRU(64→64) + Linear head，MSE 学 20 日前瞻收益的截面 rank pct
防泄露：与 lgbm_all 同纪律——24m 训练 / 6m 预测滚动、purge 21 交易日、
       T+1→T+21 标签；特征仅用 ≤t 信息（序列窗口 [t-19, t]）
评估：OOS rank IC vs lgbm_all_score 基线逐日对比
输出：factor/gru_seq_score 湖 + reports/gru_seq_oos_ic.csv
CPU 训练（本机 NVML 不可用，无 GPU 加速），预计数十分钟级。
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
import torch
import torch.nn as nn

from lgbm_all import (FACTOR_DIR, HORIZON, MIRROR, PREDICT_MONTHS, PURGE_TD,
                      TRAIN_MONTHS, q)

SEQ = 20
TOPK = 64
HIDDEN = 64
EPOCHS = 2
BATCH = 4096
LR = 1e-3
SEED = 42
torch.manual_seed(SEED)
np.random.seed(SEED)


def main():
    t0 = time.time()
    # 特征：lgbm 重要性 Top-K
    imp = pd.read_csv(_root / "reports" / "lgbm_all_importance.csv",
                      index_col=0).iloc[:, 0]
    feats = list(imp.sort_values(ascending=False).head(TOPK).index)
    print(f"特征 Top{TOPK}: {feats[:5]} ...", flush=True)

    # 特征张量 [day, code, k]，rank pct 缺失 0.5
    days_all, codes_all, mats = None, None, []
    for name in feats:
        fv = q(f"""
            SELECT CAST(date AS DATE) AS date, code, value
            FROM read_parquet('{str(FACTOR_DIR / name).replace(chr(92), "/")}/*.parquet')
            WHERE value IS NOT NULL AND isfinite(value)
        """)
        fv["date"] = pd.to_datetime(fv["date"])
        pv = fv.pivot_table(index="date", columns="code", values="value")
        pv = pv.rank(axis=1, pct=True)
        if days_all is None:
            days_all = pv.index
            codes_all = pv.columns
        else:
            pv = pv.reindex(index=days_all, columns=codes_all)
        mats.append(pv.to_numpy(dtype="float32"))
    X = np.stack(mats, axis=2)          # [T, N, K]
    X = np.nan_to_num(X, nan=0.5)
    T, N, K = X.shape
    day_idx = {d: i for i, d in enumerate(days_all)}
    code_idx = {c: i for i, c in enumerate(codes_all)}
    print(f"特征张量 {X.shape}", flush=True)

    # 标签
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
    kl = kl[kl["code"].isin(code_idx)]
    kl["ti"] = kl["date"].map(day_idx)
    kl["ni"] = kl["code"].map(code_idx)
    kl = kl.dropna(subset=["ti", "ni"])
    kl["ti"] = kl["ti"].astype(int)
    kl["ni"] = kl["ni"].astype(int)
    kl = kl[kl["ti"] >= SEQ - 1]
    print(f"标签样本 {len(kl)}", flush=True)

    class GRUModel(nn.Module):
        def __init__(self):
            super().__init__()
            self.gru = nn.GRU(K, HIDDEN, num_layers=1, batch_first=True)
            self.head = nn.Linear(HIDDEN, 1)

        def forward(self, x):                # x: [B, SEQ, K]
            _, h = self.gru(x)
            return self.head(h[-1]).squeeze(-1)

    all_days_ts = pd.Series(days_all)
    start = pd.Timestamp("2024-01-01")
    end = kl["date"].max()
    results, n_win = [], 0
    while start < end:
        pred_end = start + pd.DateOffset(months=PREDICT_MONTHS)
        train_start = start - pd.DateOffset(months=TRAIN_MONTHS)
        tr = kl[(kl["date"] >= train_start) & (kl["date"] < start)]
        te = kl[(kl["date"] >= start) & (kl["date"] < pred_end)]
        # purge：训练尾部 21 交易日
        tr_days = np.sort(tr["date"].unique())
        if len(tr_days):
            pos_last = day_idx.get(tr_days[-1])
            cutoff_day = days_all[max(pos_last - PURGE_TD, 0)]
            tr = tr[tr["date"] <= cutoff_day]
        if len(tr) < 100000 or len(te) < 20000:
            print(f"  窗口 {start.date()}: 样本不足跳过", flush=True)
            start = pred_end
            continue
        n_win += 1
        torch.manual_seed(SEED)
        model = GRUModel()
        opt = torch.optim.Adam(model.parameters(), lr=LR)
        # 训练
        tr_t = tr[["ti", "ni"]].to_numpy()
        y_tr = tr["fwd"].to_numpy(dtype="float32")
        n = len(tr_t)
        for ep in range(EPOCHS):
            perm = np.random.permutation(n)
            tot, nb = 0.0, 0
            for i in range(0, n, BATCH):
                idx = perm[i:i + BATCH]
                tis, nis = tr_t[idx, 0], tr_t[idx, 1]
                # 序列窗口 [ti-SEQ+1, ti]
                xb = np.stack([X[t - SEQ + 1:t + 1, ci, :] for t, ci in
                               zip(tis, nis)])          # [B, SEQ, K]
                xb = torch.from_numpy(xb)
                yb = torch.from_numpy(y_tr[idx])
                opt.zero_grad()
                pred = model(xb)
                loss = nn.functional.mse_loss(pred, yb)
                loss.backward()
                opt.step()
                tot += float(loss)
                nb += 1
            print(f"  窗口{n_win} epoch{ep+1}: loss {tot/max(nb,1):.5f}",
                  flush=True)
        # 预测
        model.eval()
        preds = []
        te_t = te[["ti", "ni"]].to_numpy()
        with torch.no_grad():
            for i in range(0, len(te_t), BATCH):
                idx = te_t[i:i + BATCH]
                xb = np.stack([X[t - SEQ + 1:t + 1, ci, :] for t, ci in
                               zip(idx[:, 0], idx[:, 1])])
                preds.append(model(torch.from_numpy(xb)).numpy())
        te = te.copy()
        te["score"] = np.concatenate(preds)
        results.append(te[["date", "code", "score"]])
        print(f"  窗口{n_win}: train {len(tr)} → pred {len(te)}", flush=True)
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
    print(f"GRU   IC {ics.mean():+.4f} / ICIR {ics.mean()/ics.std():+.3f} / "
          f"win {(ics>0).mean():.2f}")
    print(f"基线  IC {ics_b.mean():+.4f} / ICIR {ics_b.mean()/ics_b.std():+.3f}")
    print(f"逐日差（GRU−基线）：均值 {d.mean():+.4f}，胜率 {(d>0).mean():.2f}")
    ics_y = pd.DataFrame({"ic": ics})
    ics_y["year"] = pd.to_datetime(ics_y.index).year
    print(ics_y.groupby("year")["ic"].agg(["mean", "count"]).round(4).to_string())

    from quantlab.data.store import Store
    store = Store()
    out["date"] = out["date"].dt.date
    store.factor_dir("gru_seq_score").mkdir(parents=True, exist_ok=True)
    out.to_parquet(str(store.factor_dir("gru_seq_score") / "part-b00001.parquet"),
                   index=False)
    ics.reset_index().rename(columns={0: "ic"}).to_csv(
        _root / "reports" / "gru_seq_oos_ic.csv", index=False)
    print(f"\nscore 已落湖 gru_seq_score：{len(out)} 行；完成 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
