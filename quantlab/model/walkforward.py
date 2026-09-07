"""样本外验证 / walk-forward（L3 模型层增强）
=====================================
把回测按时间切成样本内（训练/验证）与样本外（测试）两段，
对比两端表现以检验策略是否过拟合。样本内表现远优于样本外
通常意味着过拟合。

用法：
    from quantlab.model.walkforward import split_backtest
    in_res, out_res = split_backtest(score, split_date="2025-01-01")
"""
from __future__ import annotations

import pandas as pd

from ..config import get_logger

log = get_logger(__name__)


def split_backtest(score: pd.DataFrame, split_date: str,
                   n_stocks: int = 100, rebalance: int = 20,
                   method: str = "inverse_vol") -> tuple[dict, dict]:
    """样本内/样本外分段回测

    返回 (in_sample, out_sample) 两个 run_optimized_backtest 结果。
    """
    from ..optimize.backtest import run_optimized_backtest
    in_res = run_optimized_backtest(
        score, n_stocks=n_stocks, rebalance=rebalance,
        method=method, end=split_date)
    out_res = run_optimized_backtest(
        score, n_stocks=n_stocks, rebalance=rebalance,
        method=method, start=split_date)
    return in_res, out_res


def compare_summary(in_res: dict, out_res: dict) -> pd.DataFrame:
    """样本内/样本外绩效对比表"""
    rows = []
    for label, r in (("样本内", in_res), ("样本外", out_res)):
        m = r["metrics"]
        rows.append({
            "segment": label,
            "annual_return": round(m["annual_return"] * 100, 2),
            "sharpe": m["sharpe"],
            "max_drawdown": round(m["max_drawdown"] * 100, 2),
            "excess_annual": round(r["excess"]["excess_annual"] * 100, 2),
            "information_ratio": r["excess"]["information_ratio"],
        })
    return pd.DataFrame(rows)


def multi_split_walkforward(score: pd.DataFrame,
                            split_dates: list[str],
                            hold_days: int = 252,
                            n_stocks: int = 100, rebalance: int = 20,
                            method: str = "inverse_vol") -> pd.DataFrame:
    """多切分点样本外验证

    对每个 split 点，取其后 hold_days（约 1 年）作为样本外窗口，
    汇总各窗口的样本外绩效，检验策略在不同时段的稳健性。
    """
    from ..optimize.backtest import run_optimized_backtest
    rows = []
    for split in split_dates:
        end = pd.to_datetime(split) + pd.Timedelta(days=hold_days)
        try:
            r = run_optimized_backtest(
                score, n_stocks=n_stocks, rebalance=rebalance,
                method=method, start=split, end=str(end.date()))
        except RuntimeError as e:
            rows.append({"split": split, "annual_return": np.nan,
                         "sharpe": np.nan, "excess_annual": np.nan})
            continue
        m = r["metrics"]
        rows.append({
            "split": split,
            "annual_return": round(m["annual_return"] * 100, 2),
            "sharpe": m["sharpe"],
            "excess_annual": round(r["excess"]["excess_annual"] * 100, 2),
        })
    return pd.DataFrame(rows)
