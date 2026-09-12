"""候选模型 A/B 补充评估：盈亏平衡成本 + 转移系数 TC（P0-2 + E1 落地）
======================================================================
承接 scripts/rerun_candidates_20260912.py（新口径 A/B 重跑）的两项增量：

1. 盈亏平衡成本（P0-2，从既有 A/B 结果解析，不重跑回测）：
     年总换手   = turnover_avg × (252/rebalance)
     毛超额     = 净超额 + 年总换手 × COST_PER_TURNOVER
     盈亏平衡成本 = 毛超额 / 年总换手    （每单位换手可承受成本，bp）
   对照引擎成本基线 COST_PER_TURNOVER=35bp（万2.5×2+印花税千1+滑点10bp×2）。
   安全边际 = 盈亏平衡成本 / 35bp − 1。

2. 转移系数 TC（E1，Grinold-Kahn IR=IC·√BR·TC，需重跑拿逐期权重）：
     TC_t = corr(score 截面, 主动权重)，主动权重=持仓权重−全池等权
   由 quantlab/optimize/backtest.py 引擎内置仪器化收集（tc_avg）。
   TC 低 = top100 截断/约束吃掉转移效率——"IC 好≠组合好"的量化分解。

只跑 FOCUS(2025-05-01~) 窗口 6 模型（TC 用于候选横向比较）。
输出: reports/candidate_ab_tc_20260912.{json,md}
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

AB_JSON = ROOT / "reports" / "candidate_ab_20260912.json"
OUT_JSON = ROOT / "reports" / "candidate_ab_tc_20260912.json"
OUT_MD = ROOT / "reports" / "candidate_ab_tc_20260912.md"

REBALANCE = 20
FOCUS_START = "2025-05-01"
END = "2026-09-11"
PERIODS_PER_YEAR = 252 / REBALANCE          # ≈ 12.6 期/年

TC_MODELS = ["PROD@FOCUS", "EQ3@FOCUS", "PROD_SI@FOCUS", "V3_SI@FOCUS",
             "PROD_HF@FOCUS", "PROD_HFA@FOCUS", "EQ3_HFA_ICW@FOCUS"]


def breakeven_table() -> pd.DataFrame:
    """从既有 A/B 结果算盈亏平衡成本（不重跑回测）"""
    from quantlab.model.backtest import COST_PER_TURNOVER

    d = json.loads(AB_JSON.read_text(encoding="utf-8"))
    rows = []
    for key, r in d["results"].items():
        turn, excess = r["turnover"], r["excess"]
        ann_turn = turn * PERIODS_PER_YEAR            # 年总换手（L1/2 口径）
        cost_paid = ann_turn * COST_PER_TURNOVER       # 引擎已扣成本
        gross = excess + cost_paid
        be_bp = gross / ann_turn * 1e4 if ann_turn > 0 else float("nan")
        rows.append({
            "model": key, "excess_net": round(excess * 100, 1),
            "excess_gross": round(gross * 100, 1),
            "turnover_period": round(turn * 100, 0),
            "turnover_annual": round(ann_turn * 100, 0),
            "breakeven_bp": round(be_bp, 1),
            "engine_cost_bp": round(COST_PER_TURNOVER * 1e4, 1),
            "safety_margin": round(be_bp / (COST_PER_TURNOVER * 1e4) - 1, 2),
        })
    return pd.DataFrame(rows)


def tc_table() -> pd.DataFrame:
    """重跑 FOCUS 窗口拿逐期 TC（复用 rerun 脚本的评分构建）"""
    from quantlab.optimize.backtest import run_optimized_backtest

    spec = importlib.util.spec_from_file_location(
        "rerun_cand", ROOT / "scripts" / "rerun_candidates_20260912.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    scores = mod.build_scores()

    rows = []
    for key in TC_MODELS:
        if key not in scores:
            print(f"  [skip] {key} 无评分")
            continue
        t0 = time.time()
        try:
            r = run_optimized_backtest(scores[key].copy(), n_stocks=100,
                                       rebalance=REBALANCE, method="inverse_vol",
                                       start=FOCUS_START, end=END)
        except Exception as e:
            print(f"  {key:20s} 回测失败: {str(e)[:90]}")
            continue
        m, ex = r["metrics"], r["excess"]
        rows.append({
            "model": key,
            "annual": round(m["annual_return"] * 100, 1),
            "sharpe": round(m["sharpe"], 2),
            "excess": round(ex["excess_annual"] * 100, 1),
            "ir": round(ex["information_ratio"], 3),
            "turnover": round(r["turnover_avg"] * 100, 0),
            "tc_avg": r["tc_avg"],
            "tc_periods": r["tc_n_periods"],
        })
        print(f"  {key:20s} TC {r['tc_avg']:.4f}  "
              f"({r['tc_n_periods']}期, {time.time()-t0:.0f}s)")
    return pd.DataFrame(rows)


def main() -> None:
    t0 = time.time()
    print("── 1/2 盈亏平衡成本（解析既有 A/B 结果）──")
    be = breakeven_table()
    print(be.to_string(index=False))

    print("\n── 2/2 转移系数 TC（重跑 FOCUS 窗口）──")
    tc = tc_table()

    # ── 汇总输出 ──────────────────────────────────────────────
    merged = be.merge(tc, on="model", how="left",
                     suffixes=("", "_r"))
    md = [
        "# 候选模型 A/B 补充评估：盈亏平衡成本 + 转移系数",
        "",
        f"生成：{pd.Timestamp.now():%Y-%m-%d %H:%M}　窗口：FOCUS {FOCUS_START}~{END}"
        f"　n=100 / reb={REBALANCE} / inverse_vol / 新口径",
        "",
        "## 盈亏平衡成本（全部模型，解析自 candidate_ab_20260912.json）",
        "",
        "| 模型 | 净超额% | 毛超额% | 期换手% | 年换手% | 盈亏平衡bp | 引擎成本bp | 安全边际 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for _, r in be.iterrows():
        md.append(f"| {r['model']} | {r['excess_net']} | {r['excess_gross']} | "
                  f"{r['turnover_period']:.0f} | {r['turnover_annual']:.0f} | "
                  f"{r['breakeven_bp']} | {r['engine_cost_bp']} | "
                  f"{'✅' if r['safety_margin'] > 0 else '❌'} {r['safety_margin']:+.0%} |")
    md += ["",
           "口径：毛超额=净超额+年换手×35bp（引擎已扣成本还原）；"
           "盈亏平衡bp=毛超额/年换手，即每单位换手可承受成本；"
           "安全边际=盈亏平衡/引擎成本−1。",
           "",
           "## 转移系数 TC（FOCUS 窗口重跑）",
           "",
           "| 模型 | 年化% | 夏普 | 超额% | IR | 换手% | TC | 期数 |",
           "|---|---|---|---|---|---|---|---|"]
    for _, r in tc.iterrows():
        md.append(f"| {r['model']} | {r['annual']} | {r['sharpe']} | "
                  f"{r['excess']} | {r['ir']} | {r['turnover']:.0f} | "
                  f"{r['tc_avg']} | {r['tc_periods']} |")
    md += ["",
           "TC = 逐期 corr(score 截面, 主动权重) 均值（Spearman），"
           "主动权重=持仓权重−全池等权（等权基准近似，真实基准为中证1000）。"
           "Grinold-Kahn：IR = IC·√BR·TC——TC 把『IC 好≠组合好』分解到"
           "信号→权重转移环节：top100 截断、缓冲、可成交性约束都吃 TC。",
           "",
           "*盈亏平衡与安全边际为 R2 维度（可用闸门）；TC 为 E1 维度（终审分解）。*"]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")

    OUT_JSON.write_text(json.dumps({
        "generated": pd.Timestamp.now().isoformat(timespec="seconds"),
        "breakeven": be.to_dict("records"),
        "tc": tc.to_dict("records"),
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n已写入 {OUT_JSON}\n已写入 {OUT_MD}（总耗时 {time.time()-t0:.0f}s）")


if __name__ == "__main__":
    main()
