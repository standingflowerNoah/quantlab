"""引擎口径隔离检查：run_backtest vs run_optimized_backtest(equal) 同分数同窗口对比"""
import sys
sys.path.insert(0, '.')
import warnings
warnings.filterwarnings("ignore")

from quantlab.model import build_composite, run_backtest
from quantlab.optimize.backtest import run_optimized_backtest

BASE = ["amplitude_20", "max_return_20", "momentum_120", "momentum_20",
        "momentum_60", "price_position_250", "reversal_10", "reversal_5",
        "rsi_14", "size", "total_mcap", "turnover", "volatility_20",
        "volatility_60"]

score = build_composite(BASE, universe="ashare_ex")

for start in ("2022-07-01", "2022-01-04", "2025-06-01"):
    a = run_backtest(score.copy(), n_stocks=100, rebalance=20, start=start)
    b = run_optimized_backtest(score.copy(), n_stocks=100, rebalance=20,
                               method="equal", start=start)
    ma, mb = a["metrics"], b["metrics"]
    print(f"start={start}: run_backtest 年化{ma['annual_return']:+.1%} (换手{a['turnover_avg']:.0%}) | "
          f"optimized(equal) 年化{mb['annual_return']:+.1%} (换手{b['turnover_avg']:.0%})")
