"""组合权重方案（L4 优化层）
=====================================
纯 numpy 实现，输入为历史日收益矩阵（T×n，T=观察期，n=标的数），
输出归一化权重 w（n 维，Σw=1，w≥0）。

方案：
- equal       等权（基线）
- inverse_vol 逆波动率加权（风险平价一阶近似，稳健）
- min_var     最小方差（协方差收缩 + 解析解）
- risk_parity 风险平价（乘性迭代）
"""
from __future__ import annotations

import numpy as np


def equal_weights(n: int) -> np.ndarray:
    return np.ones(n) / n


def inverse_vol_weights(hist_returns: np.ndarray) -> np.ndarray:
    """w ∝ 1/σ（逆波动率）"""
    vol = np.nanstd(hist_returns, axis=0)
    vol = np.where(vol < 1e-8, 1e-8, vol)
    w = 1.0 / vol
    return w / w.sum()


def ewma_cov(hist_returns: np.ndarray, halflife: int = 20) -> np.ndarray:
    """指数加权协方差矩阵（近期收益权重更高，半衰期 halflife 日）"""
    T = hist_returns.shape[0]
    n = hist_returns.shape[1]
    if T < 2:
        var = np.var(hist_returns, axis=0) if T == 1 else np.ones(n)
        return np.diag(np.maximum(var, 1e-8))
    lam = 0.5 ** (1.0 / halflife)
    w = lam ** np.arange(T - 1, -1, -1)
    w = w / w.sum()
    mean = np.average(hist_returns, axis=0, weights=w)
    r = hist_returns - mean
    return (r * w[:, None]).T @ r


def min_variance_weights(hist_returns: np.ndarray,
                         shrinkage: float = 0.3,
                         halflife: int = 20) -> np.ndarray:
    """最小方差：EWMA 协方差向对角收缩，解析解 w = Σ⁻¹1 / (1'Σ⁻¹1)"""
    n = hist_returns.shape[1]
    cov = ewma_cov(hist_returns, halflife)
    cov = (1 - shrinkage) * cov + shrinkage * np.diag(np.diag(cov))
    try:
        inv = np.linalg.inv(cov)
        w = inv @ np.ones(n)
        w = w / w.sum()
    except np.linalg.LinAlgError:
        return equal_weights(n)
    # 仅做多（w≥0），剔除负权重后重新归一
    w = np.clip(w, 0, None)
    if w.sum() <= 1e-12:
        return equal_weights(n)
    return w / w.sum()


def risk_parity_weights(hist_returns: np.ndarray, n_iter: int = 40,
                        halflife: int = 20) -> np.ndarray:
    """风险平价：乘性迭代使各标的的风险贡献相等（EWMA 协方差）"""
    n = hist_returns.shape[1]
    cov = ewma_cov(hist_returns, halflife)
    w = np.ones(n) / n
    for _ in range(n_iter):
        port_var = float(w @ cov @ w)
        rc = w * (cov @ w)            # 各标的风险贡献
        target = port_var / n
        denom = np.clip(rc, 1e-12, None)
        w = w * np.sqrt(np.clip(target, 1e-12, None) / denom)
        w = w / w.sum()
    return w


WEIGHT_METHODS = {
    "equal": equal_weights,
    "inverse_vol": inverse_vol_weights,
    "min_var": min_variance_weights,
    "risk_parity": risk_parity_weights,
}


def optimize_weights(method: str, hist_returns: np.ndarray) -> np.ndarray:
    """按方案名计算权重（hist_returns: T×n 收益矩阵）"""
    fn = WEIGHT_METHODS.get(method)
    if fn is None:
        raise ValueError(f"未知权重方案: {method}（可选 {list(WEIGHT_METHODS)}）")
    if method == "equal":
        return fn(hist_returns.shape[1])
    return fn(hist_returns)
