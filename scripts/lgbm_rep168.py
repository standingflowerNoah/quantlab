"""全因子库 LightGBM 建模（qlib Alpha158 范式适配版）
======================================================================
专业做法来源（qlib 官方 benchmark + 华泰 AI 系列）：
- 标签：T+1 → T+21 的 20 日收益（避免 T 日收盘价偷价；qlib 官方用次日收益
  的同一动机），横截面 rank pct 标准化（学相对排名，消除市场涨跌）
- 特征：全部 258 因子每日截面 rank pct；缺失填 0.5（rank 中位=平均水平，
  对齐 qlib Fillna 精神）；inf→nan
- 防泄露：训练窗统计量不碰预测窗；purge 21 交易日（训练尾部样本的
  21 日标签跨界预测窗 → 剔除）
- 滚动：train 24 个月 + 每 6 个月重训练（qlib Rolling 精神），2024-01 起 OOS
- 超参：qlib 官方 benchmark（强正则：lambda_l1 205/l2 580, max_depth 8）
评估：OOS rank IC + 多相位回测（top100/20日/扣费/中证1000）vs 等权基线。
输出：reports/lgbm_all_20260907.json + factor/lgbm_rep168_score 湖
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

ROOT = Path(__file__).resolve().parent.parent
LAKE = ROOT / "data" / "lake"
FACTOR_DIR = LAKE / "factor"
MIRROR = LAKE / "clean" / "mirror" / "kline_daily.parquet"

TRAIN_MONTHS = 24
PREDICT_MONTHS = 6
PURGE_TD = 21          # 训练窗尾部剔除天数（=标签 horizon，防跨界泄露）
HORIZON = 20
MIN_COV = 0.30         # 因子最低覆盖率（有效样本比例），低于则弃用该因子

import duckdb
_con = duckdb.connect()


def q(sql):
    return _con.execute(sql).df()


def load_all_factors(factor_names) -> pd.DataFrame:
    """全因子 rank pct 宽表长格式构建（float32 省内存）"""
    cols = {}
    for i, name in enumerate(factor_names):
        pattern = str(FACTOR_DIR / name / "*.parquet").replace("\\", "/")
        if not glob.glob(pattern):
            continue
        try:
            fv = q(f"""
                SELECT CAST(date AS DATE) AS date, code, rank_pct FROM (
                    SELECT date, code,
                           rank() OVER (PARTITION BY CAST(date AS DATE)
                                        ORDER BY value) * 1.0 /
                           NULLIF(COUNT(value) OVER (PARTITION BY
                                  CAST(date AS DATE)), 0) AS rank_pct
                    FROM read_parquet('{pattern}')
                    WHERE value IS NOT NULL AND isfinite(value)
                ) WHERE rank_pct IS NOT NULL
            """)
        except Exception as e:
            print(f"  跳过 {name}: {e}", flush=True)
            continue
        cols[name] = fv.set_index(["date", "code"])["rank_pct"].astype("float32")
        if (i + 1) % 40 == 0:
            print(f"  特征构建 {i+1}/{len(factor_names)}", flush=True)
    feat = pd.DataFrame(cols)
    return feat


def main():
    t0 = time.time()
    import json as _json
    _cl = _json.load(open(ROOT / "reports" / "factor_clusters_20260907.json",
                          encoding="utf-8"))
    factor_names = sorted(r["rep"] for r in _cl["representatives"])
    print(f"聚类代表 {len(factor_names)} 个，构建特征矩阵...", flush=True)

    # 1) 全因子 rank pct
    feat = load_all_factors(factor_names)
    # 覆盖率过滤：每因子的非缺失比例（按 (date,code) 总骨架）
    n_total = len(feat)
    cov = feat.notna().mean()
    keep = cov[cov >= MIN_COV].index.tolist()
    drop = cov[cov < MIN_COV].index.tolist()
    print(f"特征矩阵 {feat.shape}，覆盖率≥{MIN_COV:.0%} 保留 {len(keep)} 个"
          f"（弃用 {len(drop)} 低覆盖）", flush=True)
    feat = feat[keep].astype("float32")
    feat = feat.fillna(0.5).reset_index()     # qlib Fillna 精神：0.5=中位
    feat["date"] = pd.to_datetime(feat["date"])

    # 2) 标签：T+1 → T+21 20 日收益（防偷价），横截面 rank pct
    kl = q(f"""
        SELECT CAST(date AS DATE) AS date, code,
               LEAD(close * adj_factor, 1) OVER p AS c1,
               LEAD(close * adj_factor, {HORIZON + 1}) OVER p AS c{HORIZON+1}
        FROM read_parquet('{str(MIRROR).replace(chr(92), "/")}')
        WINDOW p AS (PARTITION BY code ORDER BY date)
    """)
    kl["fwd"] = kl[f"c{HORIZON+1}"] / kl["c1"] - 1
    kl = kl.dropna(subset=["fwd"])
    kl["fwd"] = kl.groupby("date")["fwd"].rank(pct=True)   # 横截面排名
    kl["date"] = pd.to_datetime(kl["date"])
    data = feat.merge(kl[["date", "code", "fwd"]], on=["date", "code"],
                      how="inner")
    print(f"样本 {len(data)} 行，日期 {data['date'].min().date()} ~ "
          f"{data['date'].max().date()}", flush=True)

    # 3) walk-forward 训练（qlib 官方超参）
    import lightgbm as lgb
    feats = keep
    start = pd.Timestamp("2024-01-01")
    end = data["date"].max()
    all_days = np.sort(data["date"].unique())
    day_pos = {d: i for i, d in enumerate(all_days)}
    results, imp_acc = [], {}
    n_win = 0
    while start < end:
        pred_end = start + pd.DateOffset(months=PREDICT_MONTHS)
        train_start = start - pd.DateOffset(months=TRAIN_MONTHS)
        tr = data[(data["date"] >= train_start) & (data["date"] < start)]
        te = data[(data["date"] >= start) & (data["date"] < pred_end)]
        # purge：训练尾部 21 交易日的样本标签跨界预测窗 → 剔除
        if len(tr):
            ti = day_pos.get(tr["date"].max())
            if ti is not None:
                cutoff = all_days[max(ti - PURGE_TD, 0)]
                tr = tr[tr["date"] <= cutoff]
        if len(tr) < 100000 or len(te) < 50000:
            print(f"  窗口 {start.date()}: 样本不足跳过", flush=True)
            start = pred_end
            continue
        model = lgb.LGBMRegressor(
            n_estimators=400, learning_rate=0.0421, max_depth=8,
            num_leaves=210, colsample_bytree=0.8879, subsample=0.8789,
            subsample_freq=1, reg_alpha=205.7, reg_lambda=580.98,
            min_child_samples=200, random_state=42, n_jobs=-1, verbosity=-1)
        model.fit(tr[feats], tr["fwd"])
        te = te.copy()
        te["score"] = model.predict(te[feats])
        results.append(te[["date", "code", "score"]])
        for f, v in zip(feats, model.feature_importances_):
            imp_acc[f] = imp_acc.get(f, 0) + v
        n_win += 1
        print(f"  窗口 {n_win}: train {len(tr)} → pred {len(te)} "
              f"({start.date()} ~ {min(pred_end, end).date()})", flush=True)
        start = pred_end

    if not results:
        raise RuntimeError("无有效预测窗口")
    out = pd.concat(results, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])

    # 4) OOS rank IC
    fwd_full = kl[["date", "code", "fwd"]]
    m = out.merge(fwd_full, on=["date", "code"], how="inner")
    ics = m.groupby("date").apply(
        lambda g: g["score"].rank().corr(g["fwd"].rank()),
        include_groups=False).dropna()
    print(f"\n=== OOS rank IC（2024-01 起，{len(ics)} 日）===")
    print(f"IC {ics.mean():+.4f} / ICIR {ics.mean()/ics.std():+.3f} / "
          f"win {(ics > 0).mean():.2f}")
    ics_y = pd.DataFrame({"ic": ics})
    ics_y["year"] = pd.to_datetime(ics_y.index).year
    print(ics_y.groupby("year")["ic"].agg(["mean", "count"]).round(4).to_string())

    imp = pd.Series(imp_acc).sort_values(ascending=False)
    print("\n=== Top20 特征重要性（累计）===")
    print((imp.head(20) / imp.sum()).round(4).to_string())

    # 5) 落湖供回测
    from quantlab.data.store import Store
    store = Store()
    out["date"] = out["date"].dt.date
    import duckdb as _ddb
    store.factor_dir("lgbm_rep168_score").mkdir(parents=True, exist_ok=True)
    out.to_parquet(str(store.factor_dir("lgbm_rep168_score") / "part-b00001.parquet"),
                   index=False)
    print(f"\nscore 已落湖 lgbm_rep168_score：{len(out)} 行")
    ics.reset_index().rename(columns={0: "ic"}).to_csv(
        ROOT / "reports" / "lgbm_rep168_oos_ic.csv", index=False)
    imp.head(50).to_csv(ROOT / "reports" / "lgbm_rep168_importance.csv")
    print(f"完成 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
