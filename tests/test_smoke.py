#!/usr/bin/env python3
"""核心模块冒烟测试（回归保护）
=====================================
快速验证五层核心函数不因迭代而被破坏，< 1 分钟跑完。
用法: python tests/test_smoke.py
"""
import sys
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd


class TestDataLayer(unittest.TestCase):
    def test_query_kline(self):
        from quantlab.data.store import query
        df = query("SELECT COUNT(*) n FROM kline_daily")
        self.assertGreater(int(df.iloc[0, 0]), 5_000_000)

    def test_query_finance(self):
        from quantlab.data.store import query
        df = query("SELECT COUNT(*) n FROM finance_snapshot")
        self.assertGreater(int(df.iloc[0, 0]), 5000)


class TestFactorLayer(unittest.TestCase):
    def test_list_factors(self):
        from quantlab.factor import list_factors
        df = list_factors()
        self.assertGreaterEqual(len(df), 13)

    def test_read_factor(self):
        from quantlab.data.store import Store
        fv = Store().read_factor("momentum_20")
        self.assertFalse(fv.empty)
        self.assertEqual(set(fv.columns), {"date", "code", "value"})

    def test_compute_factor(self):
        from quantlab.factor import compute_factor
        df = compute_factor("reversal_5", save=False,
                            start="2026-08-01")
        self.assertFalse(df.empty)
        self.assertIn("value", df.columns)


class TestModelLayer(unittest.TestCase):
    def test_build_composite(self):
        from quantlab.model import build_composite
        score = build_composite(["reversal_5", "size", "turnover"],
                                universe="ashare_ex")
        self.assertFalse(score.empty)
        self.assertEqual(set(score.columns), {"date", "code", "score"})

    def test_backtest_returns_excess(self):
        from quantlab.model import build_composite, run_backtest
        score = build_composite(["reversal_5", "size", "turnover"],
                                universe="ashare_ex")
        r = run_backtest(score, n_stocks=100, rebalance=20)
        self.assertIn("excess", r)
        self.assertIn("information_ratio", r["excess"])


class TestOptimizeLayer(unittest.TestCase):
    def test_weights_normalize(self):
        from quantlab.optimize.weights import optimize_weights
        rng = np.random.default_rng(0)
        hist = rng.normal(0, 0.02, (60, 50))
        for method in ["equal", "inverse_vol", "min_var", "risk_parity"]:
            w = optimize_weights(method, hist)
            self.assertAlmostEqual(float(w.sum()), 1.0, places=6)
            self.assertTrue((w >= -1e-9).all())

    def test_ewma_cov_shape(self):
        from quantlab.optimize.weights import ewma_cov
        rng = np.random.default_rng(1)
        hist = rng.normal(0, 0.02, (40, 20))
        cov = ewma_cov(hist)
        self.assertEqual(cov.shape, (20, 20))


class TestDecisionLayer(unittest.TestCase):
    def test_generate_target(self):
        from quantlab.model import build_composite
        from quantlab.decision import generate_target
        score = build_composite(["reversal_5", "size", "turnover"],
                                universe="ashare_ex")
        tgt = generate_target(score, n_stocks=50, apply_timing=False)
        self.assertGreater(len(tgt), 0)
        self.assertAlmostEqual(float(tgt["weight"].sum()), 1.0, places=3)
        self.assertLessEqual(float(tgt["weight"].max()), 0.051)

    def test_diff_orders(self):
        from quantlab.decision import diff_orders
        target = pd.DataFrame({
            "code": ["600519", "000001"],
            "name": ["茅台", "平安"],
            "weight": [0.6, 0.4],
        })
        orders = diff_orders({}, target)
        self.assertEqual(len(orders), 2)
        self.assertTrue((orders["action"] == "买入").all())


class TestResearchTools(unittest.TestCase):
    """迭代中新增的研究工具冒烟测试"""

    def test_factor_correlation(self):
        from quantlab.factor import factor_correlation
        cm = factor_correlation(["reversal_5", "size", "turnover"],
                                sample_dates=30)
        self.assertEqual(cm.shape, (3, 3))
        # 对角线应为 1
        self.assertAlmostEqual(float(cm.loc["size", "size"]), 1.0, places=1)

    def test_ic_decay(self):
        from quantlab.factor.quality import factor_ic_decay
        df = factor_ic_decay("momentum_20", horizons=[5, 20])
        self.assertGreater(len(df), 0)
        self.assertIn("icir", df.columns)

    def test_neutralize(self):
        from quantlab.data.store import Store
        from quantlab.factor.neutralize import neutralize_section
        store = Store()
        fv = store.read_factor("reversal_5")
        ind = store.q(
            "SELECT code, industry FROM instruments WHERE industry IS NOT NULL")
        size = store.read_factor("size")
        out = neutralize_section(fv.head(200000), dict(zip(ind["code"], ind["industry"])),
                                 size, mode="industry")
        self.assertFalse(out.empty)
        self.assertEqual(set(out.columns), {"date", "code", "value"})

    def test_market_timing(self):
        from quantlab.decision.timing import market_timing
        pos = market_timing()
        self.assertFalse(pos.empty)
        self.assertTrue(pos["position"].isin([0.0, 0.5, 1.0]).all())

    def test_apply_timing(self):
        from quantlab.decision.timing import apply_timing
        curve = pd.DataFrame({
            "date": pd.to_datetime(["2026-08-01", "2026-08-03", "2026-08-05"]),
            "ret": [0.01, -0.02, 0.03],
            "bench": [0.0, 0.0, 0.0],
            "nav": [1.01, 0.99, 1.02],
            "nav_bm": [1.0, 1.0, 1.0],
        })
        pos = pd.DataFrame({
            "date": pd.to_datetime(["2026-08-01", "2026-08-03", "2026-08-05"]),
            "position": [1.0, 0.5, 0.5],
        })
        out = apply_timing(curve, pos)
        self.assertAlmostEqual(float(out.loc[1, "ret"]), -0.01, places=6)

    def test_quality_factors(self):
        from quantlab.factor import compute_factor, list_factors
        names = [r["name"] for r in list_factors().to_dict("records")]
        for f in ["roe", "op_margin", "debt_ratio", "ocf_ratio"]:
            self.assertIn(f, names)
        df = compute_factor("roe", save=False)
        self.assertFalse(df.empty)
        self.assertEqual(set(df.columns), {"date", "code", "value"})

    def test_data_quality(self):
        from quantlab.data.quality import check_all
        df = check_all()
        self.assertIsInstance(df, pd.DataFrame)
        self.assertGreaterEqual(len(df), 1)

    def test_lgbm_import(self):
        import lightgbm  # noqa: F401
        from quantlab.model.lgbm import build_lgbm_score, build_lgbm_score_walkforward
        self.assertTrue(callable(build_lgbm_score))
        self.assertTrue(callable(build_lgbm_score_walkforward))

    def test_signal_tracker(self):
        import pandas as pd
        from quantlab.decision.tracker import (record_signal, signal_dates,
                                               paper_nav, paper_summary)
        df = pd.DataFrame({"code": ["600519"], "name": ["贵州茅台"],
                           "weight": [1.0]})
        record_signal(df, date="2026-09-03")   # 幂等覆盖已有快照
        self.assertIn(pd.Timestamp("2026-09-03"), signal_dates())
        self.assertIsInstance(paper_nav(), pd.DataFrame)
        s = paper_summary(paper_nav())
        self.assertGreaterEqual(s["n_snapshots"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
