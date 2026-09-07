"""LightGBM 因子合成（ML 对比实验）
=====================================
用 LightGBM 从多因子截面特征预测未来收益，输出合成评分。
严格按时间切分训练/测试，避免数据泄露。

设计原则：
- 特征 = 各因子每日截面 rank pct（与等权合成一致，尺度统一）
- 标签 = 未来 horizon 日收益（前复权）
- 训练段 → 测试段时间切分，测试段完全样本外
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import get_logger
from ..data.store import Store

log = get_logger(__name__)


def _load_features(factor_names: list[str], store: Store) -> pd.DataFrame:
    """读因子并每日截面 rank pct，合成宽表 date/code + 各因子列"""
    frames = []
    for name in factor_names:
        fv = store.read_factor(name)
        if fv.empty:
            continue
        fv["date"] = pd.to_datetime(fv["date"])
        fv["value"] = pd.to_numeric(fv["value"], errors="coerce")
        fv = fv[np.isfinite(fv["value"])]
        fv[name] = fv.groupby("date")["value"].rank(pct=True)
        frames.append(fv[["date", "code", name]])
    if not frames:
        raise RuntimeError("无可用的特征因子")
    feat = frames[0]
    for f in frames[1:]:
        feat = feat.merge(f, on=["date", "code"], how="inner")
    return feat


def _fwd_return_df(horizon: int, store: Store) -> pd.DataFrame:
    """未来 horizon 日收益（date/code/fwd），DuckDB 窗口函数"""
    df = store.q(f"""
        SELECT date, code, c_lead / c - 1 AS fwd
        FROM (
            SELECT date, code, close * adj_factor AS c,
                   LEAD(close * adj_factor, {horizon}) OVER (
                       PARTITION BY code ORDER BY date) AS c_lead
            FROM kline_daily
        )
        WHERE c_lead IS NOT NULL AND c > 0
    """)
    df["date"] = pd.to_datetime(df["date"])
    return df


def build_lgbm_score(factor_names: list[str], horizon: int = 20,
                     train_end: str = "2024-12-31",
                     test_start: str = "2025-01-01",
                     n_estimators: int = 300,
                     learning_rate: float = 0.05,
                     num_leaves: int = 63) -> pd.DataFrame:
    """训练 LightGBM 预测未来收益，返回测试段 date/code/score

    score 为模型预测的未来收益（越大越看多），可直接接入选股回测。
    """
    import lightgbm as lgb
    store = Store()
    feat = _load_features(factor_names, store)
    fwd = _fwd_return_df(horizon, store)
    data = feat.merge(fwd, on=["date", "code"], how="inner")
    data = data.dropna(subset=["fwd"])
    # 去掉标签极端值（涨跌停/停牌异常），winsorize 到 ±50%
    data["fwd"] = data["fwd"].clip(-0.5, 0.5)

    train_end = pd.Timestamp(train_end)
    test_start = pd.Timestamp(test_start)
    tr = data[data["date"] < train_end]
    te = data[data["date"] >= test_start]

    if len(tr) < 1000 or len(te) < 1000:
        raise RuntimeError(f"样本不足：train={len(tr)} test={len(te)}")

    feats = list(factor_names)
    model = lgb.LGBMRegressor(
        n_estimators=n_estimators, learning_rate=learning_rate,
        num_leaves=num_leaves, min_child_samples=100,
        subsample=0.8, colsample_bytree=0.8,
        subsample_freq=1, reg_alpha=0.1, reg_lambda=1.0,
        random_state=42, n_jobs=-1, verbosity=-1)
    log.info(f"LightGBM 训练：train={len(tr)} 样本, {len(feats)} 特征")
    model.fit(tr[feats], tr["fwd"])
    te = te.copy()
    te["score"] = model.predict(te[feats])
    log.info(f"LightGBM 预测完成：test={len(te)} 样本")

    # 特征重要性
    imp = sorted(zip(feats, model.feature_importances_),
                 key=lambda x: -x[1])
    log.info("Top 特征: " + ", ".join(f"{n}({v})" for n, v in imp[:5]))

    return te[["date", "code", "score"]]


def build_lgbm_score_walkforward(factor_names: list[str], horizon: int = 20,
                                 train_years: int = 2,
                                 predict_months: int = 6,
                                 predict_from: str = "2024-01-01",
                                 n_estimators: int = 300,
                                 learning_rate: float = 0.05,
                                 num_leaves: int = 63) -> pd.DataFrame:
    """滚动重训练 LightGBM（walk-forward）

    每 predict_months 重训练一次，用前 train_years 数据训练、预测未来窗口，
    拼接成完整预测序列。相比单次训练，模型能适应市场 regime 变化。
    """
    import lightgbm as lgb
    store = Store()
    feat = _load_features(factor_names, store)
    fwd = _fwd_return_df(horizon, store)
    data = feat.merge(fwd, on=["date", "code"], how="inner")
    data = data.dropna(subset=["fwd"])
    data["fwd"] = data["fwd"].clip(-0.5, 0.5)

    feats = list(factor_names)
    start = pd.Timestamp(predict_from)
    end = data["date"].max()
    results = []
    n_win = 0
    while start < end:
        train_start = start - pd.DateOffset(years=train_years)
        predict_end = start + pd.DateOffset(months=predict_months)
        tr = data[(data["date"] >= train_start) & (data["date"] < start)]
        te = data[(data["date"] >= start) & (data["date"] < predict_end)]
        if len(tr) < 1000 or len(te) < 1000:
            start = predict_end
            continue
        model = lgb.LGBMRegressor(
            n_estimators=n_estimators, learning_rate=learning_rate,
            num_leaves=num_leaves, min_child_samples=100,
            subsample=0.8, colsample_bytree=0.8, subsample_freq=1,
            reg_alpha=0.1, reg_lambda=1.0, random_state=42,
            n_jobs=-1, verbosity=-1)
        model.fit(tr[feats], tr["fwd"])
        te = te.copy()
        te["score"] = model.predict(te[feats])
        results.append(te[["date", "code", "score"]])
        n_win += 1
        start = predict_end
    log.info(f"LightGBM walk-forward 完成：{n_win} 个重训练窗口")
    if not results:
        raise RuntimeError("walk-forward 无有效窗口")
    return pd.concat(results, ignore_index=True)
