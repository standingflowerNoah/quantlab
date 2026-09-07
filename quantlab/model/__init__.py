"""L3 模型层：多因子合成 + 截面选股回测"""
from .composite import build_composite, FACTOR_DIRECTION
from .backtest import run_backtest, run_multiphase_backtest, performance
from .gtja import build_gtja_score

__all__ = ["build_composite", "run_backtest", "run_multiphase_backtest",
           "performance", "FACTOR_DIRECTION", "build_gtja_score"]
