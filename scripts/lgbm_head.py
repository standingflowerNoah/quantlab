"""头部锐化 ML 实验：分位数回归 + 二分类（ML 证据链最后一块）
======================================================================
背景：batch12 证明 MSE 回归的头部区分力不足（IC +0.049 但组合 −48.5pp）。
本实验直接优化头部：
  方案 A（quantile）：LightGBM objective=quantile, alpha=0.9 —— 拟合收益
          条件分布的 90% 分位，专注右尾（强势股）
  方案 B（binary）：标签 = fwd rank pct ≥ 0.8 的二分类 —— 直接学"什么
          样本的因子组合画像会进入明日头部 20%"
特征：168 聚类代表集；滚动训练范式同 lgbm_rep168。
评估（关键指标 = 头部区分力，非 IC）：
  head_ret：每日 score top100 的平均 fwd20 —— 直接对应组合收益来源
  对照：LGBM_168 回归版、PROD_SI 等权（同指标）
输出：reports/lgbm_head_20260907.json
"""
import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import glob

import numpy as np
import pandas as pd
import duckdb

ROOT = Path(__file__).resolve().parent.parent
FACTOR_DIR = ROOT / "data" / "lake" / "factor"
MIRROR = ROOT / "data/lake/clean/mirror/kline_daily.parquet"

TRAIN_MONTHS = 24
PREDICT_MONTHS = 6
PURGE_TD = 21
HORIZON = 20

import duckdb as _ddb
_con = _ddb.connect()


def q(sql):
    return _con.execute(sql).df()


def load_features(reps) -> pd.DataFrame:
    cols = {}
    for i, name in enumerate(reps):
        pattern = str(FACTOR_DIR / name / "*.parquet").replace("\\", "/")
        if not glob.glob(pattern):
            continue
        try:
            fv = q(f"""
                SELECT CAST(date AS DATE) AS date, code, rp FROM (
                    SELECT date, code,
                           rank() OVER (PARTITION BY CAST(date AS DATE)
                                        ORDER BY value) * 1.0 /
                           NULLIF(COUNT(value) OVER (PARTITION BY
                                  CAST(date AS DATE)), 0) AS rp
                    FROM read_parquet('{pattern}')
                    WHERE value IS NOT NULL AND isfinite(value)
                ) WHERE rp IS NOT NULL
            """)
        except Exception as e:
            print(f"  跳过 {name}: {e}", flush=True)
            continue
        cols[name] = fv.set_index(["date", "code"])["rp"].astype("float32")
        if (i + 1) % 50 == 0:
            print(f"  特征 {i+1}/{len(reps)}", flush=True)
    feat = pd.DataFrame(cols)
    return feat.fillna(0.5).reset_index()


def main():
    t0 = time.time()
    import json as _json
    _cl = _json.load(open(ROOT / "reports" / "factor_clusters_20260907.json",
                          encoding="utf-8"))
    reps = sorted(r["rep"] for r in _cl["representatives"])
    print(f"特征 = {len(reps)} 聚类代表，构建...", flush=True)
    feat = load_features(reps)
    feat["date"] = pd.to_datetime(feat["date"])

    # 标签：T+1 → T+21 20 日收益（连续 + 横截面 rank 两个版本）
    kl = q(f"""
        SELECT CAST(date AS DATE) AS date, code,
               LEAD(close * adj_factor, 1) OVER p AS c1,
               LEAD(close * adj_factor, {HORIZON+1}) OVER p AS c21
        FROM read_parquet('{str(MIRROR).replace(chr(92), "/")}')
        WINDOW p AS (PARTITION BY code ORDER BY date)
    """)
    kl["fwd"] = kl["c21"] / kl["c1"] - 1
    kl = kl.dropna(subset=["fwd"])
    kl["fwd"] = kl["fwd"].clip(-0.5, 0.5)
    kl["date"] = pd.to_datetime(kl["date"])
    # 横截面 rank pct（分类标签用）
    kl["fwd_rank"] = kl.groupby("date")["fwd"].rank(pct=True)
    data = feat.merge(kl[["date", "code", "fwd", "fwd_rank"]],
                      on=["date", "code"], how="inner")
    print(f"样本 {len(data)} 行", flush=True)

    import lightgbm as lgb
    all_days = np.sort(data["date"].unique())
    day_pos = {d: i for i, d in enumerate(all_days)}
    start = pd.Timestamp("2024-01-01")
    end = data["date"].max()

    out_frames = {"quantile": [], "binary": []}
    n_win = 0
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
        base_kw = dict(n_estimators=400, learning_rate=0.0421, max_depth=8,
                       num_leaves=210, colsample_bytree=0.8879,
                       subsample=0.8789, subsample_freq=1,
                       reg_alpha=205.7, reg_lambda=580.98,
                       min_child_samples=200, random_state=42,
                       n_jobs=-1, verbosity=-1)
        # A: 分位数回归（90% 分位，专注右尾）
        ma = lgb.LGBMRegressor(objective="quantile", alpha=0.9, **base_kw)
        ma.fit(tr[reps], tr["fwd"])
        ta = te.copy()
        ta["score"] = ma.predict(te[reps])
        out_frames["quantile"].append(ta[["date", "code", "score"]])
        # B: 二分类（明日头部 20%）
        mb = lgb.LGBMClassifier(objective="binary", **base_kw)
        mb.fit(tr[reps], (tr["fwd_rank"] >= 0.8).astype(int))
        tb = te.copy()
        tb["score"] = mb.predict_proba(te[reps])[:, 1]
        out_frames["binary"].append(tb[["date", "code", "score"]])
        n_win += 1
        print(f"  窗口 {n_win}: train {len(tr)} → pred {len(te)}", flush=True)
        start = pred_end

    # 评估：OOS IC + 头部收益（top100 平均 fwd）
    fwd_full = kl[["date", "code", "fwd"]]
    report = {}
    for tag, frames in out_frames.items():
        sc = pd.concat(frames, ignore_index=True)
        sc["date"] = pd.to_datetime(sc["date"])
        m = sc.merge(fwd_full, on=["date", "code"], how="inner")
        ics = m.groupby("date").apply(
            lambda g: g["score"].rank().corr(g["fwd"].rank()),
            include_groups=False).dropna()
        head = (m.sort_values(["date", "score"], ascending=[True, False])
                 .groupby("date").head(100)
                 .groupby("date")["fwd"].mean())
        report[tag] = {
            "ic": round(float(ics.mean()), 4),
            "icir": round(float(ics.mean() / ics.std()), 3),
            "head_ret": round(float(head.mean()), 4),
            "head_ann": round(float(head.mean() * 12), 4),
            "n_days": int(len(ics)),
        }
        print(f"\n[{tag}] IC {report[tag]['ic']:+.4f} / "
              f"ICIR {report[tag]['icir']:+.3f} | "
              f"top100 平均 fwd20 {report[tag]['head_ret']:+.4f} "
              f"(月化年化 {report[tag]['head_ann']:+.1%})", flush=True)
        sc.to_parquet(ROOT / "reports" / f"lgbm_head_{tag}_score.parquet",
                      index=False)

    # 对照：LGBM_168 回归版 与 PROD_SI 的同指标
    reg = pd.read_parquet(ROOT / "data/lake/factor/lgbm_rep168_score/"
                          "part-b00001.parquet")
    reg["date"] = pd.to_datetime(reg["date"])
    reg["score"] = reg.groupby("date")["score"].rank(pct=True)
    m = reg.merge(fwd_full, on=["date", "code"], how="inner")
    ics = m.groupby("date").apply(
        lambda g: g["score"].rank().corr(g["fwd"].rank()),
        include_groups=False).dropna()
    head = (m.sort_values(["date", "score"], ascending=[True, False])
             .groupby("date").head(100).groupby("date")["fwd"].mean())
    report["reg168"] = {"ic": round(float(ics.mean()), 4),
                        "icir": round(float(ics.mean()/ics.std()), 3),
                        "head_ret": round(float(head.mean()), 4),
                        "head_ann": round(float(head.mean()*12), 4),
                        "n_days": int(len(ics))}
    print(f"\n[reg168] IC {report['reg168']['ic']:+.4f} | "
          f"top100 平均 fwd20 {report['reg168']['head_ret']:+.4f} "
          f"(月化年化 {report['reg168']['head_ann']:+.1%})", flush=True)

    from quantlab.model import build_composite
    prod = build_composite(["size", "amihud_20", "sue_i", "overnight_mom_20"],
                           universe="ashare_ex")
    prod["date"] = pd.to_datetime(prod["date"])
    m = prod.merge(fwd_full, on=["date", "code"], how="inner")
    m["score"] = m.groupby("date")["score"].rank(pct=True)
    head = (m.sort_values(["date", "score"], ascending=[True, False])
             .groupby("date").head(100).groupby("date")["fwd"].mean())
    report["PROD_SI"] = {"head_ret": round(float(head.mean()), 4),
                         "head_ann": round(float(head.mean()*12), 4),
                         "n_days": int(len(head))}
    print(f"[PROD_SI] top100 平均 fwd20 {report['PROD_SI']['head_ret']:+.4f} "
          f"(月化年化 {report['PROD_SI']['head_ann']:+.1%})", flush=True)

    import json
    json.dump(report, open(ROOT / "reports" / "lgbm_head_20260907.json", "w",
                           encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n完成 {time.time()-t0:.0f}s → reports/lgbm_head_20260907.json")


if __name__ == "__main__":
    main()
